"""Pacing limiter tests. The sliding window is the one that matters —
verify it blocks a burst after an idle period."""

from __future__ import annotations

import time

import pytest

from src.data.pacing import PacingLimiter


def test_refuses_rate_above_ibkr_ceiling():
    with pytest.raises(ValueError, match="exceeds IBKR"):
        PacingLimiter(requests_per_minute=20)


def test_sliding_window_blocks_burst_even_with_full_bucket():
    """An idle limiter must not permit 60 requests at once."""
    lim = PacingLimiter(requests_per_minute=6, max_requests=5, window_seconds=600)
    lim._tokens = 999.0  # simulate a long idle period refilling the bucket

    for _ in range(5):
        lim.acquire(timeout=1)

    # Sixth must block on the sliding window, not the bucket.
    with pytest.raises(TimeoutError):
        lim.acquire(timeout=0.5)

    assert lim.stats()["requests_in_window"] == 5


def test_token_bucket_smooths_rate():
    """At 6/min a second request must wait ~10s, so a legal limiter still
    paces rather than firing back to back."""
    lim = PacingLimiter(requests_per_minute=6)
    lim._tokens = 1.0
    lim.acquire(timeout=1)          # consumes the only token
    with pytest.raises(TimeoutError):
        lim.acquire(timeout=2)      # refill takes ~10s, so 2s is not enough


def test_cooldown_blocks_everything():
    lim = PacingLimiter(requests_per_minute=6)
    lim.enter_cooldown(seconds=30)
    assert lim.in_cooldown
    with pytest.raises(TimeoutError):
        lim.acquire(timeout=0.2)


def test_window_evicts_old_requests():
    lim = PacingLimiter(requests_per_minute=6, max_requests=3, window_seconds=0.3)
    lim._tokens = 99.0
    for _ in range(3):
        lim.acquire(timeout=1)
    assert lim.stats()["requests_in_window"] == 3
    time.sleep(0.4)
    assert lim.stats()["requests_in_window"] == 0


def test_estimate_matches_ibkr_reality():
    """150 symbols at 6/min should be ~25 minutes, as the spec claims."""
    lim = PacingLimiter(requests_per_minute=6)
    assert lim.estimate_seconds(150) == pytest.approx(1500, rel=0.01)
    assert lim.estimate_seconds(500) / 60 == pytest.approx(83.3, rel=0.01)
