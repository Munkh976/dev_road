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
        conn.execute(
            """INSERT INTO system_state (id, trading_enabled, updated_at)
               VALUES (1, 1, '2026-01-01T00:00:00+00:00')"""
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
    # Cross-site protection is our own guard; see the tests at the bottom.
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


async def test_reject_risk_rejected_is_allowed_and_not_an_override(ds, db_path):
    _add_proposal(db_path, "p1", risk_status="REJECT")
    # Agreeing with the risk engine is following the system: no reason needed.
    resp = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "REJECT"})
    assert resp.status_code == 200
    assert resp.json()["override"] is False
    assert _status(db_path, "p1") == "REJECTED"
    assert _q(db_path, "SELECT override, override_reason FROM approvals") == [(0, None)]


async def test_reject_clean_proposal_still_needs_a_reason(ds, db_path):
    # Guards against over-loosening: rejecting a clean, unflagged proposal is
    # still a deviation from the system.
    _add_proposal(db_path, "p1", risk_status="PASS", ai_flag=0)
    resp = await _post(ds, "/-/approve", {"proposal_id": "p1", "decision": "REJECT"})
    assert resp.status_code == 400
    assert _status(db_path, "p1") == "PENDING_APPROVAL"
    assert _n_approvals(db_path) == 0


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


# --- cross-site POST protection -------------------------------------------
# Datasette's CSRF check only applies to cookie-bearing requests, and there is
# no login, so without our own guard any web page open in the browser could
# POST to localhost. Browsers attach Origin / Sec-Fetch-Site to such requests
# and page scripts cannot forge them.

EVIL = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
ROUTE_BODIES = {
    "/-/approve": {"proposal_id": "p1", "decision": "APPROVE"},
    "/-/approve-all": {},
    "/-/halt": {"reason": "x"},
    "/-/journal": {"entry": "x"},
}


def _writes(path: Path) -> tuple:
    return (
        _q(path, "SELECT status FROM proposals ORDER BY proposal_id"),
        _n_approvals(path),
        _q(path, "SELECT trading_enabled FROM system_state"),
        _q(path, "SELECT COUNT(*) FROM journal"),
    )


@pytest.mark.parametrize("route", list(ROUTE_BODIES))
async def test_cross_site_post_is_403_and_writes_nothing(ds, db_path, route):
    _add_proposal(db_path, "p1")
    before = _writes(db_path)
    resp = await ds.client.post(route, data=ROUTE_BODIES[route], headers=EVIL)
    assert resp.status_code == 403
    assert _writes(db_path) == before


async def test_cross_origin_header_alone_is_403(ds, db_path):
    # Older browsers omit Sec-Fetch-Site; a foreign Origin must still fail.
    _add_proposal(db_path, "p1")
    resp = await ds.client.post(
        "/-/approve-all", data={}, headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert _status(db_path, "p1") == "PENDING_APPROVAL"


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl / scripts: no browser headers at all
        {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost"},
        {"Sec-Fetch-Site": "none"},  # user-initiated, e.g. address bar
    ],
)
async def test_same_origin_post_still_works(ds, db_path, headers):
    _add_proposal(db_path, "p1")
    resp = await ds.client.post("/-/approve-all", data={}, headers=headers)
    assert resp.status_code == 200
    assert _status(db_path, "p1") == "APPROVED"


async def test_same_site_but_cross_origin_is_403(ds, db_path):
    # A different localhost port is "same-site" but not same-origin.
    _add_proposal(db_path, "p1")
    resp = await ds.client.post(
        "/-/approve-all", data={},
        headers={"Sec-Fetch-Site": "same-site", "Origin": "http://localhost:9999"},
    )
    assert resp.status_code == 403
    assert _status(db_path, "p1") == "PENDING_APPROVAL"


# --- DNS rebinding: Host allowlist on every request -------------------------
# A hostile domain pointed at 127.0.0.1 makes localhost:8001 same-origin for
# that page, so Origin/Sec-Fetch-Site checks alone would pass. The Host header
# cannot be forged by page scripts, so it is allowlisted on ALL requests.

EVIL_HOST = {"Host": "evil.example:8001"}


@pytest.mark.parametrize(
    "path",
    ["/", "/operations", "/operations/proposals.json", "/-/versions.json"],
)
async def test_non_local_host_get_is_403(ds, db_path, path):
    _add_proposal(db_path, "p1")
    resp = await ds.client.get(path, headers=EVIL_HOST)
    assert resp.status_code == 403
    assert "p1" not in resp.text  # nothing about the data leaked


async def test_non_local_host_post_is_403_and_writes_nothing(ds, db_path):
    _add_proposal(db_path, "p1")
    resp = await ds.client.post(
        "/-/approve-all", data={},
        # Origin matches Host, exactly what a rebinding page would send.
        headers={**EVIL_HOST, "Origin": "http://evil.example:8001"},
    )
    assert resp.status_code == 403
    assert _status(db_path, "p1") == "PENDING_APPROVAL"
    assert _n_approvals(db_path) == 0


@pytest.mark.parametrize(
    "host",
    [
        "localhost", "localhost:8001",
        "127.0.0.1", "127.0.0.1:8001",
        "[::1]", "[::1]:8001",
        "LOCALHOST:8001",
    ],
)
async def test_local_hosts_are_allowed(ds, db_path, host):
    resp = await ds.client.get("/-/versions.json", headers={"Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "host",
    ["localhost.evil.example", "evil.example", "127.0.0.1.evil.example:8001",
     "[::2]", "0.0.0.0:8001", ""],
)
async def test_lookalike_and_empty_hosts_are_refused(ds, host):
    resp = await ds.client.get("/-/versions.json", headers={"Host": host})
    assert resp.status_code == 403
