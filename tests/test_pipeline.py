"""
pipeline.py decides what to store, what to alert on, and what to swallow.

Every bug here was found by a throwaway script and none of it was preserved.
The three that mattered most -- an unimplemented cooldown, a Telegram outage
aborting the cycle, and a bare `except Exception` hiding programming errors --
each have a test below.
"""

import threading
import time

import psycopg2
import pytest
import requests

from pipeline import (Pipeline, MarketInactive, MarketNeverTraded,
                      TRADE_REFRESH_EVERY_CYCLES, depth_warning)

from conftest import FakeKalshi, FakeStorage, Recorder, config_for, healthy_market, stdev_alert, trade


def build(markets, kalshi=None, storage=None, notify=None, **cfg):
    """Assemble a Pipeline over fakes; **cfg overrides config_for fields."""
    k = kalshi or FakeKalshi(markets={m["ticker"]: m for m in markets})
    s = storage or FakeStorage()
    p = Pipeline(k, s, config_for([m["ticker"] for m in markets], **cfg), notify=notify or Recorder())
    return p, k, s, p.notify


# ── storing and silence ──────────────────────────────────────────────

class TestPolling:
    """A cycle stores a tick, stays quiet until warm, and survives bad markets."""

    def test_stores_a_tick_each_cycle(self):
        p, k, s, _ = build([healthy_market("A")])
        p.poll_once()
        p.poll_once()
        assert s.ticks["A"] == [0.50, 0.50]

    def test_stays_silent_while_warming_up(self):
        p, k, s, rec = build([healthy_market("A")], notify=Recorder())
        for _ in range(11):
            p.poll_once()
        assert rec.messages == []

    def test_inserts_before_checking(self):
        """check_pct reads prices[0] as current, so the tick must already exist."""
        p, k, s, _ = build([healthy_market("A")])
        p.poll_once()
        assert s.calls.index("insert_tick") < s.calls.index("get_recent")

    def test_inactive_market_is_reported(self):
        markets = [healthy_market("A", status="closed")]
        p, k, s, rec = build(markets, notify=Recorder())
        p.poll_once()
        assert len(rec.messages) == 1
        assert "status is 'closed'" in rec.messages[0]
        assert "A" not in s.ticks

    def test_never_traded_market_is_reported(self):
        markets = [healthy_market("A", last_price=0.0, volume=0.0)]
        p, k, s, rec = build(markets, notify=Recorder())
        p.poll_once()
        assert "never traded" in rec.messages[0]

    def test_zero_price_with_volume_is_accepted(self):
        """A real trade at 0.0 with volume is data, not an absence of it."""
        markets = [healthy_market("A", last_price=0.0, volume=500.0)]
        p, k, s, rec = build(markets, notify=Recorder())
        p.poll_once()
        assert s.ticks["A"] == [0.0]
        assert rec.messages == []

    def test_one_bad_market_does_not_stop_the_others(self):
        markets = [healthy_market("A", status="closed"),
                   healthy_market("B"),
                   healthy_market("C", last_price=0.0, volume=0.0)]
        p, k, s, rec = build(markets, notify=Recorder())
        p.poll_once()
        assert "B" in s.ticks
        assert "A" not in s.ticks

    def test_problem_notices_are_deduped(self):
        markets = [healthy_market("A", status="closed")]
        p, k, s, rec = build(markets, notify=Recorder())
        for _ in range(20):
            p.poll_once()
        assert len(rec.messages) == 1, "should notify once, not every cycle"


# ── cooldown ─────────────────────────────────────────────────────────

class TestCooldown:
    """A sustained move alerts once, not on every poll."""

    def _warmed(self, notify=None, **cfg):
        """Poll 11 flat cycles so the lookback window has a baseline."""
        markets = [healthy_market("A")]
        s = FakeStorage()
        p, k, _, rec = build(markets, storage=s, notify=notify or Recorder(), **cfg)
        for _ in range(11):
            p.poll_once()
        return p, k, s, rec

    def test_a_sustained_move_alerts_exactly_once(self):
        """Regression: _deliver once sent unconditionally, so a still-true
        condition fired on every single poll."""
        p, k, s, rec = self._warmed()
        k.markets["A"] = healthy_market("A", last=0.30)
        sent = [p.poll_once() for _ in range(6)]
        assert len(rec.messages) == 1
        assert all(r == [] for r in sent[1:]), "later cycles should suppress"

    def test_the_suppressed_message_is_still_the_same_alert(self):
        p, k, s, rec = self._warmed()
        k.markets["A"] = healthy_market("A", last=0.30)
        p.poll_once()
        p.poll_once()
        assert "-40.0%" in rec.messages[0]

    def test_a_different_ticker_is_not_suppressed(self):
        markets = [healthy_market("A"), healthy_market("B")]
        p, k, s, rec = build(markets, notify=Recorder())
        for _ in range(11):
            p.poll_once()
        k.markets["A"] = healthy_market("A", last=0.30)
        k.markets["B"] = healthy_market("B", last=0.30)
        sent = p.poll_once()
        assert len(sent) == 2

    def test_zero_cooldown_disables_suppression(self):
        p, k, s, rec = self._warmed(alert_cooldown_seconds=0)
        k.markets["A"] = healthy_market("A", last=0.30)
        for _ in range(3):
            p.poll_once()
        assert len(rec.messages) == 3


# ── delivery failures ────────────────────────────────────────────────

class TestDeliveryFailure:
    """A Telegram outage must not lose the cycle, the alert, or the cooldown."""

    def test_a_failed_send_does_not_abort_the_cycle(self):
        """Regression: _deliver raised out of poll_once, so one 500 lost every
        market after the failing one for that cycle."""
        tickers = ("A", "B", "C")
        markets = [healthy_market(t) for t in tickers]
        rec = Recorder(fail_first=1)
        p, k, s, _ = build(markets, storage=FakeStorage(), notify=rec)
        for _ in range(11):
            p.poll_once()          # warm every market to a 0.50 baseline
        for t in tickers:
            k.markets[t] = healthy_market(t, last=0.30)
        sent = p.poll_once()
        assert len(rec.attempts) == 3, "all three should have been attempted"
        assert len(sent) == 2, "B and C should still have been delivered"
        assert len(rec.messages) == 2

    def test_a_failed_send_does_not_start_the_cooldown(self):
        """Otherwise a message that never arrived is silenced for 2 hours."""
        p, k, s, rec = build([healthy_market("A")], notify=Recorder(fail_first=1))
        for _ in range(11):
            p.poll_once()
        k.markets["A"] = healthy_market("A", last=0.30)
        assert p.poll_once() == []
        assert "A" not in p._last_alerted

    def test_the_failed_alert_retries_next_cycle(self):
        p, k, s, rec = build([healthy_market("A")], notify=Recorder(fail_first=1))
        for _ in range(11):
            p.poll_once()
        k.markets["A"] = healthy_market("A", last=0.30)
        p.poll_once()          # the send fails
        assert p.poll_once()   # and is retried, succeeding this time
        assert len(rec.attempts) == 2
        assert len(rec.messages) == 1


# ── exception classification ─────────────────────────────────────────

class TestExceptionClassification:
    """poll_once absorbs only expected failures; bugs must surface."""

    @pytest.mark.parametrize("exc", [
        requests.ConnectionError("timeout"),
        requests.HTTPError("500"),
        MarketInactive("closed"),
        MarketNeverTraded("never traded"),
        psycopg2.OperationalError("server closed the connection"),
    ])
    def test_expected_failures_are_absorbed(self, exc):
        k = FakeKalshi(markets={"A": healthy_market("A")})
        k.raise_on_market = exc
        p, _, s, _ = build([healthy_market("A")], kalshi=k)
        p.poll_once()  # must not raise

    @pytest.mark.parametrize("exc", [
        AttributeError("typo"),
        TypeError("bad argument"),
        NameError("undefined name"),
        KeyError("missing"),
    ])
    def test_bugs_propagate(self, exc):
        """A bug retried silently every 2 minutes is how they hide."""
        k = FakeKalshi(markets={"A": healthy_market("A")})
        k.raise_on_market = exc
        p, _, _, _ = build([healthy_market("A")], kalshi=k)
        with pytest.raises(type(exc)):
            p.poll_once()

    def test_database_error_is_re_raised_not_swallowed(self):
        k = FakeKalshi(markets={"A": healthy_market("A")})
        k.raise_on_market = psycopg2.errors.UndefinedTable("relation missing")
        p, _, _, _ = build([healthy_market("A")], kalshi=k)
        with pytest.raises(psycopg2.Error):
            p.poll_once()

    def test_connection_loss_triggers_reconnect(self):
        p, k, s, _ = build([healthy_market("A")])
        k.raise_on_market = psycopg2.OperationalError("gone")
        p.poll_once()
        assert s.reconnects == 1


# ── trade fetching and batching ──────────────────────────────────────

class TestTradeBatching:
    """Fetch trades when (and only when) the market has actually traded."""

    def test_fetches_trades_on_the_first_poll(self):
        p, k, s, _ = build([healthy_market("A")])
        p.poll_once()
        assert k.trade_fetches == 1

    def test_skips_the_fetch_when_nothing_traded(self):
        p, k, s, _ = build([healthy_market("A")])
        for _ in range(10):
            p.poll_once()
        assert k.trade_fetches == 1, "a quiet market should not refetch every cycle"

    def test_a_new_trade_triggers_a_fetch(self):
        p, k, s, _ = build([healthy_market("A")])
        p.poll_once()
        k.markets["A"] = healthy_market("A", volume=140.0)
        p.poll_once()
        assert k.trade_fetches == 2

    def test_a_same_price_trade_still_triggers_a_fetch(self):
        """Only volume moved, but a trade did occur."""
        p, k, s, _ = build([healthy_market("A")])
        p.poll_once()
        k.markets["A"] = healthy_market("A", volume=180.0)
        p.poll_once()
        assert k.trade_fetches == 2

    def test_backstop_refetches_periodically(self):
        """Volume is reported to 2dp, so a tiny trade could leave it unchanged."""
        p, k, s, _ = build([healthy_market("A")])
        p.poll_once()
        for _ in range(TRADE_REFRESH_EVERY_CYCLES):
            p.poll_once()
        assert k.trade_fetches == 2

    def test_batching_does_not_skip_the_poll_itself(self):
        p, k, s, _ = build([healthy_market("A")])
        for _ in range(10):
            p.poll_once()
        assert len(s.ticks["A"]) == 10


# ── depth warnings ───────────────────────────────────────────────────

class TestDepthWarning:
    """A market can print a price and still be too thin to alert on."""

    def test_healthy_market_is_silent(self):
        assert depth_warning(healthy_market("A")) is None

    def test_thin_volume_is_flagged(self):
        msg = depth_warning(healthy_market("A", volume_24h=0.2))
        assert "24h volume" in msg

    def test_thin_open_interest_is_flagged(self):
        assert "open interest" in depth_warning(healthy_market("A", open_interest=0.0))

    def test_thin_book_is_flagged(self):
        assert "bid size" in depth_warning(healthy_market("A", yes_bid_size=0.1))

    def test_missing_size_is_not_treated_as_thin(self):
        m = healthy_market("A")
        del m["yes_bid_size"]
        assert depth_warning(m) is None

    def test_lists_every_problem(self):
        msg = depth_warning(healthy_market("A", volume_24h=0.0, open_interest=0.0,
                                           yes_bid_size=0.0))
        assert msg.count(",") == 2


# ── stdev dispatch ───────────────────────────────────────────────────

class TestStdevDispatch:
    """The pipeline routes a stdev market to check_stdev, not check_pct."""

    def test_uses_the_trade_series(self):
        markets = [healthy_market("A")]
        s = FakeStorage(trades={"A": [0.9, 0.58, 0.66, 0.65, 0.67, 0.64, 0.63]})
        rec = Recorder()
        p, k, _, _ = build(markets, storage=s, notify=rec,
                           alert=stdev_alert(threshold=2.0, sample_size=7))
        sent = p.poll_once()
        assert len(sent) == 1 and "σ" in sent[0]
        assert "get_recent_trades" in s.calls

    def test_warm_up_notice_is_deduped(self):
        markets = [healthy_market("A")]
        rec = Recorder()
        p, k, s, _ = build(markets, notify=rec, alert=stdev_alert(sample_size=20))
        for _ in range(20):
            p.poll_once()
        assert len(rec.messages) == 1
        assert "still warming up" in rec.messages[0]


# ── loop and shutdown ────────────────────────────────────────────────

class TestLoop:
    """run_forever stops promptly and outlives a single bad cycle."""

    def test_stop_ends_run_forever(self):
        p, k, s, _ = build([healthy_market("A")], poll_interval_seconds=1)
        threading.Timer(1.5, p.stop).start()
        started = time.monotonic()
        p.run_forever()
        assert time.monotonic() - started < 5
        assert k.market_calls, "the loop should have polled before stopping"

    def test_sleep_is_interrupted_promptly(self):
        """A 2 min uninterruptible sleep would lose the race with Docker's
        10s SIGKILL grace period."""
        p, k, s, _ = build([healthy_market("A")], poll_interval_seconds=120)
        p.stop()
        started = time.monotonic()
        p._sleep_until(time.monotonic() + 120)
        assert time.monotonic() - started < 1.0

    def test_sleep_returns_at_the_deadline(self):
        p, k, s, _ = build([healthy_market("A")])
        started = time.monotonic()
        p._sleep_until(time.monotonic() + 0.3)
        assert 0.25 <= time.monotonic() - started < 2.0

    def test_a_bug_does_not_kill_the_loop(self):
        """run_forever is the last resort: log loudly, keep going."""
        p, k, s, _ = build([healthy_market("A")], poll_interval_seconds=1)
        calls = {"n": 0}
        original = p.poll_once

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise AttributeError("boom")
            return original()

        p.poll_once = flaky
        threading.Timer(2.5, p.stop).start()
        p.run_forever()
        assert calls["n"] >= 2, "loop should have continued past the failure"
