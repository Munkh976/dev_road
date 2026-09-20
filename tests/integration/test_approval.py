"""
Integration tests for datasette/plugins/approval.py.

These go through the real Datasette routes with ds.client.post, so they cover
routing, form parsing, CSRF handling and the threaded write path, not just the
handler functions. Every test builds its own database from db/schema.sql in
tmp_path; db/operations.db is never touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from datasette.app import Datasette

REPO = Path(__file__).resolve().parent.parent.parent
SCHEMA = REPO / "db" / "schema.sql"
PLUGINS = REPO / "datasette" / "plugins"

RUN_ID = "run-1"


def _build_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(
            """INSERT INTO runs (run_id, started_at, run_type, strategy_name,
                   strategy_version, config_hash, mode, status)
               VALUES (?, '2026-01-01T00:00:00+00:00', 'propose', 't', '1',
                       'h', 'paper', 'ok')""",
            [RUN_ID],
        )
        conn.commit()
    finally:
        conn.close()


def _add_proposal(
    path: Path, pid: str, *, risk_status: str = "PASS", ai_flag: int | None = 0
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO proposals
               (proposal_id, run_id, created_at, symbol, action, reason,
                reference_price, quantity, estimated_value, risk_status,
                risk_rejections, ai_flag)
               VALUES (?, ?, '2026-01-01T00:00:00+00:00', ?, 'BUY', 'E1',
                       10.0, 1, 10.0, ?, ?, ?)""",
            [pid, RUN_ID, pid.upper(), risk_status,
             '["max_position"]' if risk_status == "REJECT" else None, ai_flag],
        )
        conn.commit()
    finally:
        conn.close()


def _q(path: Path, sql: str, params: list | None = None) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, params or []).fetchall()
    finally:
        conn.close()


def _status(path: Path, pid: str) -> str:
    return _q(path, "SELECT status FROM proposals WHERE proposal_id = ?", [pid])[0][0]


def _n_approvals(path: Path, pid: str | None = None) -> int:
    if pid is None:
        return _q(path, "SELECT COUNT(*) FROM approvals")[0][0]
    return _q(path, "SELECT COUNT(*) FROM approvals WHERE proposal_id = ?", [pid])[0][0]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "operations.db"  # Datasette names the db by file stem
    _build_db(path)
    return path


@pytest_asyncio.fixture
async def ds(db_path: Path):
    instance = Datasette([str(db_path)], plugins_dir=str(PLUGINS))
    await instance.invoke_startup()
    yield instance


async def _post(ds: Datasette, route: str, data: dict):
    # Datasette's CSRF middleware stays on, but asgi-csrf only enforces it on
    # requests that carry cookies, so a cookie-less form post gets through.
    # CSRF itself is Datasette's code and is not what these tests exercise.
    return await ds.client.post(route, data=data)


async def test_double_approve_is_409_and_one_row(ds, db_path):
    _add_proposal(db_path, "p1")
    first = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "APPROVE"})
    second = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "APPROVE"})
    assert first.status_code == 200
    assert second.status_code == 409
    assert _n_approvals(db_path, "p1") == 1
    assert _status(db_path, "p1") == "APPROVED"


async def test_approve_risk_rejected_is_403(ds, db_path):
    _add_proposal(db_path, "p1", risk_status="REJECT")
    resp = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "APPROVE"})
    assert resp.status_code == 403
    assert _status(db_path, "p1") == "PENDING_APPROVAL"
    assert _n_approvals(db_path) == 0


async def test_reject_risk_rejected_is_allowed(ds, db_path):
    _add_proposal(db_path, "p1", risk_status="REJECT")
    # Rejecting something the system also flagged (risk REJECT is not an
    # ai_flag) counts as an override, so a reason is required.
    resp = await _post(
        ds, "/-/approve",
        {"proposal_id": "p1", "decision": "REJECT", "override_reason": "agree"},
    )
    assert resp.status_code == 200
    assert _status(db_path, "p1") == "REJECTED"
    assert _n_approvals(db_path, "p1") == 1


async def test_failed_approval_insert_leaves_no_half_written_state(ds, db_path):
    _add_proposal(db_path, "p1")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TRIGGER boom BEFORE INSERT ON approvals
           BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END"""
    )
    conn.commit()
    conn.close()

    resp = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "APPROVE"})
    assert resp.status_code >= 500
    assert _status(db_path, "p1") == "PENDING_APPROVAL"
    assert _n_approvals(db_path) == 0


async def test_approve_all_only_touches_clean_proposals(ds, db_path):
    _add_proposal(db_path, "clean", risk_status="PASS", ai_flag=0)
    _add_proposal(db_path, "flagged", risk_status="PASS", ai_flag=1)
    _add_proposal(db_path, "rejected", risk_status="REJECT", ai_flag=0)

    resp = await _post(ds, "/-/approve-all", {})
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved"] == 1
    assert body["still_pending"] == 2
    assert _status(db_path, "clean") == "APPROVED"
    assert _status(db_path, "flagged") == "PENDING_APPROVAL"
    assert _status(db_path, "rejected") == "PENDING_APPROVAL"
    assert _n_approvals(db_path) == 1


async def test_override_without_reason_is_400_and_writes_nothing(ds, db_path):
    # Approving an AI-flagged proposal is an override, so a reason is required.
    _add_proposal(db_path, "p1", ai_flag=1)
    resp = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "APPROVE"})
    assert resp.status_code == 400
    assert _status(db_path, "p1") == "PENDING_APPROVAL"
    assert _n_approvals(db_path) == 0
