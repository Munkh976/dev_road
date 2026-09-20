"""fetch_symbol: pacing, error 420, ADJUSTED_LAST's empty endDateTime."""

from __future__ import annotations

import pytest

from src.data.pacing import PacingLimiter
from src.data.refresh import FetchError, PacingViolation, fetch_symbol
from tests.support import FakeIB, FakeLimiter, recent_bars

PACING_MSG = "Historical Market Data Service error message: pacing violation"


@pytest.fixture
def setup():
    events: list[str] = []
    ib = FakeIB({"AAPL": recent_bars(50)}, events)
    return ib, FakeLimiter(events), events


def test_limiter_is_acquired_before_every_request(refresh_cfg, setup):
    ib, limiter, events = setup
    fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    assert events == ["acquire", "request", "acquire", "request"]


def test_end_datetime_is_empty_for_adjusted_last(refresh_cfg, setup):
    ib, limiter, _ = setup
    fetch_symbol(ib, "AAPL", "30 D", refresh_cfg, limiter)
    _, kw = ib.requests[0]
    assert kw["endDateTime"] == ""              # IBKR rejects a dated end for ADJUSTED_LAST
    assert kw["whatToShow"] == "ADJUSTED_LAST"
    assert kw["durationStr"] == "30 D"


def test_returns_dated_ohlcv_frame(refresh_cfg, setup):
    ib, limiter, _ = setup
    df = fetch_symbol(ib, "AAPL", "15 Y", refresh_cfg, limiter)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "date" and df.index.is_monotonic_increasing
    assert len(df) == 50


@pytest.mark.parametrize("code,msg", [(420, "pacing"), (162, PACING_MSG)])
def test_pacing_error_cools_down_and_does_not_retry(refresh_cfg, setup, code, msg):
    ib, limiter, events = setup
    ib.errors["AAPL"] = [(code, msg)]
    with pytest.raises(PacingViolation):
        fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    assert limiter.cooldowns == 1
    assert events == ["acquire", "request", "cooldown"]     # one request, no retry
    assert len(ib.requests) == 1


def test_pacing_error_engages_the_real_limiter(refresh_cfg):
    ib = FakeIB({"AAPL": recent_bars(50)})
    ib.errors["AAPL"] = [(420, "pacing")]
    limiter = PacingLimiter(requests_per_minute=refresh_cfg.data.ibkr_requests_per_minute)
    with pytest.raises(PacingViolation):
        fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    assert limiter.in_cooldown


def test_other_error_is_a_fetch_error_without_cooldown(refresh_cfg, setup):
    ib, limiter, _ = setup
    ib.errors["AAPL"] = [(200, "No security definition")]
    with pytest.raises(FetchError, match="200") as exc:
        fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    assert not isinstance(exc.value, PacingViolation)
    assert limiter.cooldowns == 0


def test_162_without_pacing_text_is_not_a_pacing_violation(refresh_cfg, setup):
    ib, limiter, _ = setup
    ib.errors["AAPL"] = [(162, "HMDS query returned no data")]
    with pytest.raises(FetchError) as exc:
        fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    assert not isinstance(exc.value, PacingViolation)
    assert limiter.cooldowns == 0


def test_farm_status_messages_are_ignored_when_bars_arrive(refresh_cfg, setup):
    ib, limiter, _ = setup
    ib.info_errors = [(2106, "HMDS data farm connection is OK")]
    assert len(fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)) > 0


def test_error_handler_is_always_detached(refresh_cfg, setup):
    ib, limiter, _ = setup
    fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    ib.errors["AAPL"] = [(420, "pacing")]
    with pytest.raises(PacingViolation):
        fetch_symbol(ib, "AAPL", "10 D", refresh_cfg, limiter)
    assert len(ib.errorEvent) == 0
