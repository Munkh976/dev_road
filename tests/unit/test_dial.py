"""The live risk dial (spec section 8.2): every limit is enforced in code."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.config import REPO_ROOT
from src.risk.dial import (
    CAUTION,
    NORMAL,
    DialError,
    DialState,
    current_dial,
    effective_target,
    level_target,
    set_dial,
)
from tests.mutants import load_mutant

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)      # a Monday
WHY = "Earnings week and I cannot stomach a full book"


def count(db, table):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_no_change_on_record_means_normal(db, refresh_cfg):
    state = current_dial(db, T0, refresh_cfg)
    assert (state.level, state.target) == (NORMAL, 0.85)


def test_a_change_writes_a_journal_entry_and_links_to_it(db, refresh_cfg):
    state = set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    assert state == DialState(CAUTION, 0.60, T0, T0 + timedelta(weeks=4))
    row = db.execute("SELECT * FROM risk_dial_changes").fetchone()
    assert (row["level"], row["target"], row["reason"]) == (CAUTION, 0.60, WHY)
    journal = db.execute("SELECT * FROM journal WHERE id = ?", (row["journal_id"],)).fetchone()
    assert WHY in journal["entry"] and CAUTION in journal["entry"]
    assert journal["followed_system"] == 0                    # it is an override of the system
    assert journal["week_of"] == "2026-10-05"
    assert current_dial(db, T0 + timedelta(days=1), refresh_cfg).level == CAUTION


def test_a_reason_is_required_and_a_refused_change_leaves_nothing_behind(db, refresh_cfg):
    for reason in ("", "   ", None):
        with pytest.raises(DialError, match="reason"):
            set_dial(db, CAUTION, reason, T0, refresh_cfg)
    assert count(db, "risk_dial_changes") == 0 and count(db, "journal") == 0


def test_the_defensive_level_cannot_be_set_by_hand(db, refresh_cfg):
    for level in ("defensive", "aggressive", "", "NORMAL "):
        with pytest.raises(DialError, match="only|unknown"):
            set_dial(db, level, WHY, T0, refresh_cfg)
    assert count(db, "risk_dial_changes") == 0


def test_at_most_one_change_per_30_days(db, refresh_cfg):
    set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    for days in (0, 1, 14, 29):
        with pytest.raises(DialError, match="once per 30 days"):
            set_dial(db, NORMAL, WHY, T0 + timedelta(days=days), refresh_cfg)
    assert count(db, "risk_dial_changes") == 1 and count(db, "journal") == 1
    later = T0 + timedelta(days=30)                       # the first change expired at 28 days
    assert set_dial(db, CAUTION, WHY, later, refresh_cfg).level == CAUTION
    # and the limit restarts from THAT change
    with pytest.raises(DialError, match="once per 30 days"):
        set_dial(db, NORMAL, WHY, later + timedelta(days=5), refresh_cfg)


def test_it_expires_after_four_weeks_and_reverts_to_normal(db, refresh_cfg):
    set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    just_before = T0 + timedelta(weeks=4) - timedelta(seconds=1)
    assert current_dial(db, just_before, refresh_cfg).level == CAUTION
    at = T0 + timedelta(weeks=4)
    assert (current_dial(db, at, refresh_cfg).level, current_dial(db, at, refresh_cfg).target) == (NORMAL, 0.85)
    # renewing needs a fresh, reasoned change; expiry did not count as one
    with pytest.raises(DialError, match="once per 30 days"):
        set_dial(db, CAUTION, WHY, at, refresh_cfg)
    assert set_dial(db, CAUTION, WHY, T0 + timedelta(days=30), refresh_cfg).level == CAUTION


def test_setting_the_level_it_is_already_at_is_refused(db, refresh_cfg):
    with pytest.raises(DialError, match="already"):
        set_dial(db, NORMAL, WHY, T0, refresh_cfg)


def test_time_must_be_timezone_aware(db, refresh_cfg):
    with pytest.raises(DialError, match="timezone"):
        set_dial(db, CAUTION, WHY, datetime(2026, 10, 5, 12, 0), refresh_cfg)


# --------------------------------------------------------- the effective target


def test_the_dial_only_lowers_the_target(refresh_cfg):
    normal, caution = DialState(NORMAL, 0.85), DialState(CAUTION, 0.60)
    assert effective_target(0.85, normal, refresh_cfg) == 0.85
    assert effective_target(0.85, caution, refresh_cfg) == 0.60
    assert effective_target(0.40, caution, refresh_cfg) == 0.40      # defensive is lower still
    assert effective_target(0.40, normal, refresh_cfg) == 0.40       # and the dial cannot lift it


def test_the_target_is_never_above_85_percent_whatever_the_inputs(refresh_cfg):
    absurd = DialState("normal", 0.99)
    assert effective_target(0.99, absurd, refresh_cfg) == 0.85


def test_level_targets_come_from_config(refresh_cfg):
    assert level_target(NORMAL, refresh_cfg) == 0.85
    assert level_target(CAUTION, refresh_cfg) == 0.60


def test_break_the_dial_can_raise_the_target(refresh_cfg):
    bad = load_mutant("src.risk.dial", "return min(regime_target, dial.target, cfg.regime.target_normal)",
                      "return dial.target")
    assert effective_target(0.40, DialState(NORMAL, 0.85), refresh_cfg) == 0.40
    assert bad.effective_target(0.40, DialState(NORMAL, 0.85), refresh_cfg) == 0.85


def test_break_no_monthly_limit(db, refresh_cfg):
    bad = load_mutant("src.risk.dial", "if now - last_at < wait:", "if False:")
    bad.set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    with pytest.raises(DialError):
        set_dial(db, NORMAL, WHY, T0 + timedelta(days=1), refresh_cfg)
    assert bad.set_dial(db, NORMAL, WHY, T0 + timedelta(days=1), refresh_cfg).level == NORMAL


def test_break_the_dial_never_expires(db, refresh_cfg):
    bad = load_mutant("src.risk.dial", "if now >= expires or level == NORMAL:", "if level == NORMAL:")
    set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    late = T0 + timedelta(weeks=9)
    assert current_dial(db, late, refresh_cfg).level == NORMAL
    assert bad.current_dial(db, late, refresh_cfg).level == CAUTION


def test_break_no_reason_needed(db, refresh_cfg):
    bad = load_mutant("src.risk.dial", "if not reason or not reason.strip():", "if False:")
    with pytest.raises(DialError, match="reason"):
        set_dial(db, CAUTION, "   ", T0, refresh_cfg)
    # the mutant lets a blank reason through; the table's CHECK is the last line of defense
    with pytest.raises(sqlite3.IntegrityError):
        bad.set_dial(db, CAUTION, "   ", T0, refresh_cfg)


# ------------------------------------------------------------- the table itself


def test_the_table_refuses_a_defensive_level_a_blank_reason_and_no_journal_entry(db):
    ok = ("2026-10-05T12:00:00+00:00", "caution", 0.6, "why", 1, "2026-11-02T12:00:00+00:00")
    db.execute("INSERT INTO journal (week_of, written_at, entry) VALUES ('2026-10-05','x','y')")
    sql = ("INSERT INTO risk_dial_changes (changed_at, level, target, reason, journal_id, "
           "expires_at) VALUES (?,?,?,?,?,?)")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (ok[0], "defensive", 0.4, ok[3], 1, ok[5]))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (ok[0], "caution", 0.6, "  ", 1, ok[5]))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (ok[0], "caution", 0.6, "why", None, ok[5]))
    db.execute(sql, ok)


# ------------------------------------------------------- an override, by any measure


def test_a_dial_change_counts_as_an_override_in_the_adherence_query(db, refresh_cfg):
    metadata = json.loads((REPO_ROOT / "datasette" / "metadata.json").read_text(encoding="utf-8"))
    sql = metadata["databases"]["operations"]["queries"]["adherence"]["sql"]
    assert db.execute(sql).fetchall() == []
    set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    (row,) = db.execute(sql).fetchall()
    assert (row["decisions"], row["overrides"], row["override_pct"]) == (1, 1, 100.0)
    # in the same week as an ordinary approval that was NOT an override
    db.execute("INSERT INTO runs (run_id, started_at, run_type, strategy_name, strategy_version, "
               "config_hash, mode, status) VALUES ('r','2026-10-05','propose','s','v','h','paper','ok')")
    db.execute("INSERT INTO proposals (proposal_id, run_id, created_at, symbol, action, reason, "
               "reference_price, quantity, estimated_value, status, risk_status) "
               "VALUES ('p','r','2026-10-05','AAA','BUY','ENTRY',100,10,1000,'APPROVED','PASS')")
    db.execute("INSERT INTO approvals (proposal_id, decided_at, decision, override) "
               "VALUES ('p', ?, 'APPROVE', 0)", (T0.isoformat(),))
    (row,) = db.execute(sql).fetchall()
    assert (row["decisions"], row["overrides"], row["override_pct"]) == (2, 1, 50.0)


def test_a_dial_change_appears_in_the_override_log(db, refresh_cfg):
    set_dial(db, CAUTION, WHY, T0, refresh_cfg)
    (row,) = db.execute("SELECT * FROM v_override_log").fetchall()
    assert row["symbol"] == "RISK DIAL" and row["decision"] == "DIAL" and row["override"] == 1
    assert row["override_reason"] == WHY


# ------------------------------------------------------------- live only


def test_the_backtest_and_the_planner_never_consult_the_dial():
    for path in ("src/backtest/walkforward.py", "src/strategy/plan.py", "src/strategy/rules.py",
                 "src/strategy/regime.py"):
        text = (REPO_ROOT / path).read_text(encoding="utf-8")
        assert "risk.dial" not in text and "risk_dial" not in text, path
