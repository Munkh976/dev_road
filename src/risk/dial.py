"""
The risk dial (spec section 8.2). LIVE ONLY: the backtest never reads it, and a
test holds that line.

A manual cap on the invested target, for the times a human wants less risk than
the market filter asks for. It exists so that wanting to de-risk has a sanctioned,
recorded outlet instead of an unrecorded override of individual proposals. Every
rule below exists to keep that outlet from becoming a way to trade by feel:

  * Two levels only: `normal` (85%) and `caution` (60%). The defensive level (40%)
    is set ONLY by the market filter; there is no way to ask for it here.
  * It can only LOWER the target. `effective_target` takes the minimum of the
    regime target, the dial and the normal target, so the dial can never raise
    exposure, and never above 85%.
  * At most one change per `min_days_between_changes` (30). A month is long
    enough that a change is a decision, not a reaction to this week's headline.
  * A change needs a written reason, stored as a journal entry the row points at.
  * It auto-expires after `expiry_weeks` (4) and reverts to normal. Caution has to
    be renewed on purpose; it cannot be forgotten and left in place.
  * Every change counts as an OVERRIDE in the adherence query (three consecutive
    weeks of overrides is a kill criterion, spec section 14).

Time is always passed in (`now`), never read from the clock, so every rule is
testable and the answer for a given week is reproducible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.config import Config

NORMAL = "normal"
CAUTION = "caution"
DIAL_LEVELS = (NORMAL, CAUTION)


class DialError(ValueError):
    """The requested dial change is refused. The message says which rule."""


@dataclass(frozen=True)
class DialState:
    level: str
    target: float
    changed_at: datetime | None = None
    expires_at: datetime | None = None


def level_target(level: str, cfg: Config) -> float:
    """Invested fraction a dial level asks for."""
    levels = cfg.risk_dial.levels
    if level == NORMAL:
        return levels.normal
    if level == CAUTION:
        return levels.caution
    raise DialError(
        f"unknown dial level {level!r}: only {DIAL_LEVELS} can be set by hand. "
        "The defensive level is set only by the market filter.")


def _aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise DialError("`now` must be timezone-aware (UTC)")
    return when


def _last_change(conn: sqlite3.Connection) -> sqlite3.Row | tuple | None:
    return conn.execute(
        "SELECT changed_at, level, target, expires_at FROM risk_dial_changes "
        "ORDER BY changed_at DESC, id DESC LIMIT 1").fetchone()


def current_dial(conn: sqlite3.Connection, now: datetime, cfg: Config) -> DialState:
    """The dial in force at `now`: the last change, unless it has expired, in which
    case normal. No change on record is normal."""
    now = _aware(now)
    row = _last_change(conn)
    if row is None:
        return DialState(NORMAL, level_target(NORMAL, cfg))
    changed_at, level, _, expires_at = row[0], row[1], row[2], row[3]
    changed, expires = datetime.fromisoformat(changed_at), datetime.fromisoformat(expires_at)
    if now >= expires or level == NORMAL:
        return DialState(NORMAL, level_target(NORMAL, cfg), changed, expires)
    return DialState(level, level_target(level, cfg), changed, expires)


def set_dial(
    conn: sqlite3.Connection, level: str, reason: str, now: datetime, cfg: Config,
) -> DialState:
    """Change the dial by hand, or raise `DialError` naming the rule that stops it.

    Writes the journal entry and the change in one transaction, so a change never
    exists without its reason and a reason is never left behind by a refused change.
    """
    now = _aware(now)
    target = level_target(level, cfg)               # refuses the defensive level
    if target > cfg.regime.target_normal:           # config validates this too
        raise DialError("the dial can never exceed the normal target")
    if not reason or not reason.strip():
        raise DialError("a journal reason is required for every dial change")

    last = _last_change(conn)
    if last is not None:
        wait = timedelta(days=cfg.risk_dial.min_days_between_changes)
        last_at = datetime.fromisoformat(last[0])
        if now - last_at < wait:
            raise DialError(
                f"the dial changes at most once per {cfg.risk_dial.min_days_between_changes} "
                f"days; last change {last_at.date()}, next allowed {(last_at + wait).date()}")
    if current_dial(conn, now, cfg).level == level:
        raise DialError(f"the dial is already at {level}")

    expires = now + timedelta(weeks=cfg.risk_dial.expiry_weeks)
    week_of = (now - timedelta(days=now.weekday())).date().isoformat()
    with conn:
        journal_id = conn.execute(
            "INSERT INTO journal (week_of, written_at, followed_system, entry) VALUES (?,?,?,?)",
            (week_of, now.isoformat(), 0, f"RISK DIAL -> {level}: {reason.strip()}"),
        ).lastrowid
        conn.execute(
            "INSERT INTO risk_dial_changes (changed_at, level, target, reason, journal_id, "
            "expires_at) VALUES (?,?,?,?,?,?)",
            (now.isoformat(), level, target, reason.strip(), journal_id, expires.isoformat()))
    return DialState(level, target, now, expires)


def effective_target(regime_target: float, dial: DialState, cfg: Config) -> float:
    """The invested target live actually uses: the lowest of what the market
    filter asks for, what the dial asks for, and the normal target. The dial
    lowers exposure and can never raise it."""
    return min(regime_target, dial.target, cfg.regime.target_normal)
