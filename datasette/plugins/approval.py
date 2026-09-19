"""
Datasette plugin: approve / reject / modify trade proposals in the browser.

Follows the pattern from your EDGI portal work: a register_routes hook, POST
handlers with CSRF, and writes through datasette.get_database().execute_write.

Safety properties worth keeping if you refactor this:

  * Approving writes an APPROVAL ROW. It does not place an order. A separate
    execution step reads approved proposals and submits them. That separation
    is what stops a stray browser click from reaching the market.
  * Overrides are recorded, with a reason. Three consecutive weeks of them is
    a kill criterion (spec section 14), and you cannot measure that without
    this column.
  * A proposal whose risk_status is REJECT cannot be approved at all. The
    risk engine is not advisory.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from datasette import hookimpl
from datasette.utils.asgi import Forbidden, Response


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
    ai_flagged = proposal["ai_flag"] == 1
    override = int(
        (decision == "APPROVE" and ai_flagged)
        or (decision == "REJECT" and not ai_flagged)
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

    await db.execute_write(
        """INSERT INTO approvals
           (proposal_id, decided_at, decision, modified_quantity,
            override, override_reason, decided_by)
           VALUES (?, ?, ?, ?, ?, ?, 'human')""",
        [proposal_id, _now(), decision, qty, override, override_reason],
    )
    new_status = {"APPROVE": "APPROVED", "REJECT": "REJECTED", "MODIFY": "MODIFIED"}[decision]
    await db.execute_write(
        "UPDATE proposals SET status = ? WHERE proposal_id = ?",
        [new_status, proposal_id],
    )

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
    for pid in ids:
        await db.execute_write(
            """INSERT INTO approvals
               (proposal_id, decided_at, decision, override, decided_by)
               VALUES (?, ?, 'APPROVE', 0, 'human')""",
            [pid, now],
        )
        await db.execute_write(
            "UPDATE proposals SET status = 'APPROVED' WHERE proposal_id = ?", [pid]
        )

    skipped = await db.execute(
        """SELECT COUNT(*) AS n FROM proposals
           WHERE status = 'PENDING_APPROVAL'"""
    )
    return Response.json(
        {
            "ok": True,
            "approved": len(ids),
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
        (r"^/-/approve$", approve_proposal),
        (r"^/-/approve-all$", approve_all),
        (r"^/-/halt$", halt_system),
        (r"^/-/journal$", journal_entry),
    ]
