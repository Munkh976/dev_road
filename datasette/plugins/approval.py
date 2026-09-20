"""
Datasette plugin: approve / reject / modify trade proposals in the browser.

Patterns (register_routes, atomic writes, CSRF) are documented in
docs/datasette_patterns.md.

Safety properties worth keeping if you refactor this:

  * Approving writes an APPROVAL ROW. It does not place an order. A separate
    execution step reads approved proposals and submits them. That separation
    is what stops a stray browser click from reaching the market.
  * Overrides are recorded, with a reason. Three consecutive weeks of them is
    a kill criterion (spec section 14), and you cannot measure that without
    this column.
  * A proposal whose risk_status is REJECT cannot be approved at all. The
    risk engine is not advisory. This is enforced in the write's WHERE clause,
    not only in a pre-check, so a stale read cannot slip past it.
  * Cross-site POSTs are refused (403) on every route. Datasette's own CSRF
    check only applies to requests that carry cookies, and there is no login,
    so without this any web page open in the browser could POST to localhost.
  * Every decision is one transaction (status flip + approval row). Datasette
    0.65 does not open a transaction for execute_write_fn, so the write
    functions below use `with conn:` to get commit/rollback.
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from datasette.utils.asgi import Forbidden, Response

from datasette import hookimpl

# Sec-Fetch-Site values that mean the user's own browser initiated the request
# from this origin (or directly, e.g. the address bar). Page scripts cannot
# forge this header; "same-site" (another localhost port) and "cross-site" are
# both refused.
_TRUSTED_FETCH_SITE = {"same-origin", "none"}


def _is_same_origin(request) -> bool:
    """True unless a browser-supplied header says this came from elsewhere.

    Non-browser clients (curl, scripts) send neither header and are allowed:
    the threat here is other web pages, not other programs on the machine.
    Both headers are checked when present, so one cannot vouch for the other.
    """
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in _TRUSTED_FETCH_SITE:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    # Origin "null" (sandboxed frames, some redirects) parses to an empty
    # netloc and fails this comparison, which is the safe outcome.
    return urlsplit(origin).netloc.lower() == host.lower()


def same_origin_only(handler):
    """Wrap a route handler so cross-site requests get 403 before any logic."""

    async def guarded(request, datasette):
        if not _is_same_origin(request):
            return Response.json(
                {"error": "cross-site request refused"}, status=403
            )
        return await handler(request, datasette)

    guarded.__name__ = handler.__name__
    guarded.__doc__ = handler.__doc__
    return guarded


class _AlreadyDecided(Exception):
    """The guarded status flip matched no row: decided already, or not allowed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _fetch_proposal(db, proposal_id: str):
    rows = await db.execute(
        "SELECT * FROM proposals WHERE proposal_id = ?", [proposal_id]
    )
    return rows.first()


async def approve_proposal(request, datasette):
    """POST /-/approve — record a decision on one proposal."""
    if request.method != "POST":
        return Response.text("POST only", status=405)

    post = await request.post_vars()
    proposal_id = post.get("proposal_id")
    decision = (post.get("decision") or "").upper()
    override_reason = post.get("override_reason") or None
    modified_qty = post.get("modified_quantity")

    if decision not in {"APPROVE", "REJECT", "MODIFY"}:
        return Response.json({"error": "decision must be APPROVE|REJECT|MODIFY"}, status=400)

    db = datasette.get_database("operations")
    proposal = await _fetch_proposal(db, proposal_id)
    if proposal is None:
        return Response.json({"error": "unknown proposal_id"}, status=404)

    if proposal["status"] != "PENDING_APPROVAL":
        return Response.json(
            {"error": f"proposal already {proposal['status']}"}, status=409
        )

    # The risk engine is not advisory. A rejected proposal is not approvable.
    if decision in {"APPROVE", "MODIFY"} and proposal["risk_status"] == "REJECT":
        raise Forbidden(
            "This proposal failed a risk check and cannot be approved. "
            f"Failures: {proposal['risk_rejections']}"
        )

    # An override is approving something the system flagged, or rejecting
    # something it proposed cleanly. Both are deviations worth counting.
    # Rejecting a proposal the risk engine already rejected is following the
    # system, not deviating from it, so it is never an override.
    ai_flagged = proposal["ai_flag"] == 1
    risk_rejected = proposal["risk_status"] == "REJECT"
    override = int(
        (decision == "APPROVE" and ai_flagged)
        or (decision == "REJECT" and not ai_flagged and not risk_rejected)
        or (decision == "MODIFY")
    )

    if override and not override_reason:
        return Response.json(
            {"error": "override_reason is required when deviating from the system"},
            status=400,
        )

    qty = None
    if decision == "MODIFY":
        try:
            qty = float(modified_qty)
        except (TypeError, ValueError):
            return Response.json({"error": "modified_quantity must be numeric"}, status=400)
        if qty <= 0:
            return Response.json({"error": "modified_quantity must be > 0"}, status=400)

    new_status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "MODIFY": "MODIFIED"}[decision]

    def _decide(conn):
        with conn:  # one transaction: commit on success, roll back on any error
            # Guarded flip first: only one caller can win, and a risk-REJECT
            # proposal can never become APPROVED/MODIFIED even if the Python
            # pre-check above was bypassed or read stale data.
            n = conn.execute(
                """UPDATE proposals SET status = ?
                   WHERE proposal_id = ? AND status = 'PENDING_APPROVAL'
                     AND (? = 'REJECT' OR risk_status != 'REJECT')""",
                [new_status, proposal_id, decision],
            ).rowcount
            if n != 1:
                raise _AlreadyDecided()
            conn.execute(
                """INSERT INTO approvals
                   (proposal_id, decided_at, decision, modified_quantity,
                    override, override_reason, decided_by)
                   VALUES (?, ?, ?, ?, ?, ?, 'human')""",
                [proposal_id, _now(), decision, qty, override, override_reason],
            )

    try:
        await db.execute_write_fn(_decide)
    except _AlreadyDecided:
        return Response.json({"error": "proposal already decided"}, status=409)

    return Response.json(
        {
            "ok": True,
            "proposal_id": proposal_id,
            "status": new_status,
            "override": bool(override),
            "note": "Recorded. No order has been sent — run the execution step.",
        }
    )


async def approve_all(request, datasette):
    """POST /-/approve-all — accept every clean pending proposal.

    Only touches proposals that passed risk AND were not AI-flagged, so the
    convenience path can never bulk-approve something the system warned about.
    """
    if request.method != "POST":
        return Response.text("POST only", status=405)

    db = datasette.get_database("operations")
    rows = await db.execute(
        """SELECT proposal_id FROM proposals
           WHERE status = 'PENDING_APPROVAL'
             AND risk_status = 'PASS'
             AND (ai_flag IS NULL OR ai_flag = 0)"""
    )
    ids = [r["proposal_id"] for r in rows]
    now = _now()

    def _approve_all(conn):
        approved = 0
        with conn:  # all-or-nothing: a failure leaves every proposal pending
            for pid in ids:
                # Same guard as the single path, re-checked at write time.
                n = conn.execute(
                    """UPDATE proposals SET status = 'APPROVED'
                       WHERE proposal_id = ? AND status = 'PENDING_APPROVAL'
                         AND risk_status = 'PASS'
                         AND (ai_flag IS NULL OR ai_flag = 0)""",
                    [pid],
                ).rowcount
                if n != 1:
                    continue  # decided or changed since the read; skip it
                conn.execute(
                    """INSERT INTO approvals
                       (proposal_id, decided_at, decision, override, decided_by)
                       VALUES (?, ?, 'APPROVE', 0, 'human')""",
                    [pid, now],
                )
                approved += 1
        return approved

    approved = await db.execute_write_fn(_approve_all)

    skipped = await db.execute(
        """SELECT COUNT(*) AS n FROM proposals
           WHERE status = 'PENDING_APPROVAL'"""
    )
    return Response.json(
        {
            "ok": True,
            "approved": approved,
            "still_pending": skipped.first()["n"],
            "note": "Flagged and risk-rejected proposals were left for individual review.",
        }
    )


async def halt_system(request, datasette):
    """POST /-/halt — manual kill switch. Fails closed."""
    if request.method != "POST":
        return Response.text("POST only", status=405)
    post = await request.post_vars()
    reason = post.get("reason") or "manual halt"

    db = datasette.get_database("operations")
    await db.execute_write(
        """UPDATE system_state
           SET trading_enabled = 0, halt_reason = ?, halted_at = ?,
               halted_by = 'human', updated_at = ?
           WHERE id = 1""",
        [reason, _now(), _now()],
    )
    return Response.json({"ok": True, "trading_enabled": False, "reason": reason})


async def journal_entry(request, datasette):
    """POST /-/journal — the weekly note."""
    if request.method != "POST":
        return Response.text("POST only", status=405)
    post = await request.post_vars()
    entry = post.get("entry")
    if not entry:
        return Response.json({"error": "entry is required"}, status=400)

    db = datasette.get_database("operations")
    await db.execute_write(
        """INSERT INTO journal
           (week_of, written_at, run_id, followed_system, entry, emotional_note)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            post.get("week_of") or datetime.now().strftime("%Y-%m-%d"),
            _now(),
            post.get("run_id"),
            int(post.get("followed_system", 1)),
            entry,
            post.get("emotional_note"),
        ],
    )
    return Response.json({"ok": True})


@hookimpl
def register_routes():
    return [
        (r"^/-/approve$", same_origin_only(approve_proposal)),
        (r"^/-/approve-all$", same_origin_only(approve_all)),
        (r"^/-/halt$", same_origin_only(halt_system)),
        (r"^/-/journal$", same_origin_only(journal_entry)),
    ]
