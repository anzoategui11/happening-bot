"""
Shared fixtures and fakes.

Two rules this suite follows, both of which the manual scripts that came before
it did not:

1. No test touches the network, Postgres, or Telegram. Everything runs against
   the fakes below, so a green run means something offline.
2. The autouse `no_real_io` fixture enforces rule 1. A test that accidentally
   constructs a real Storage or KalshiClient fails loudly instead of quietly
   writing to the live Supabase database.
"""

import pytest

import psycopg2
import requests

from config import AlertType, MarketAlert, PipelineConfig, markets_from_tickers


class BlockedIO(RuntimeError):
    """Raised when test code tries to reach the network or the database."""


@pytest.fixture(autouse=True)
def no_real_io(monkeypatch):
    """Fail loudly rather than let a test hit Supabase or Kalshi."""
    def blocked(*_args, **_kwargs):
        raise BlockedIO(
            "test attempted real I/O. Use the fakes from conftest "
            "(FakeStorage / FakeKalshi) or inject a fake notify."
        )

    monkeypatch.setattr(psycopg2, "connect", blocked)
    for name in ("request", "get", "post"):
        monkeypatch.setattr(requests, name, blocked)
        monkeypatch.setattr(requests.Session, name, blocked, raising=False)


# ── fakes ────────────────────────────────────────────────────────────

class FakeStorage:
    """In-memory stand-in for both series.

    Deliberately records the arguments it is given, so a test can assert which
    series a check read from -- that distinction is load-bearing and easy to
    break silently.
    """

    def __init__(self, ticks=None, trades=None):
        self.ticks = dict(ticks or {})
        self.trades = dict(trades or {})
        self.calls = []          # method names, in order
        self.inserted_trades = []
        self.reconnects = 0

    # polled series
    def insert_tick(self, ticker, price):
        self.calls.append("insert_tick")
        self.ticks.setdefault(ticker, []).insert(0, price)

    def get_recent(self, ticker, n):
        self.calls.append("get_recent")
        return self.ticks.get(ticker, [])[:n]

    # trade series
    def insert_trades(self, ticker, trades):
        self.calls.append("insert_trades")
        self.inserted_trades.append((ticker, list(trades)))
        for t in trades:
            self.trades.setdefault(ticker, []).insert(0, t["price"])
        return len(trades)

    def get_recent_trades(self, ticker, n):
        self.calls.append("get_recent_trades")
        return self.trades.get(ticker, [])[:n]

    def count_trades(self, ticker):
        return len(self.trades.get(ticker, []))

    def reconnect(self):
        self.reconnects += 1


class FakeKalshi:
    """Canned market records, and a counter so batching can be asserted."""

    def __init__(self, markets=None, trades=None):
        self.markets = dict(markets or {})
        self.trade_history = dict(trades or {})
        self.market_calls = []
        self.trade_fetches = 0
        self.raise_on_market = None

    def get_market(self, ticker):
        self.market_calls.append(ticker)
        if self.raise_on_market:
            raise self.raise_on_market
        return dict(self.markets[ticker])

    def get_trades(self, ticker, limit=200):
        self.trade_fetches += 1
        return list(self.trade_history.get(ticker, []))


def healthy_market(ticker="A", last=0.50, **overrides):
    """A market record that passes every check in _poll_market."""
    m = {
        "ticker": ticker, "status": "active", "last_price": last,
        "volume": 100.0, "volume_24h": 50.0, "open_interest": 100.0,
        "yes_bid_size": 200.0, "yes_bid": last, "yes_ask": last,
    }
    m.update(overrides)
    return m


def trade(price, trade_id=None, age_seconds=0.0):
    """A trade record shaped like KalshiClient.get_trades output."""
    from datetime import datetime, timedelta, timezone
    return {
        "trade_id": trade_id or f"t-{price}-{age_seconds}",
        "ts": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        "price": price,
    }


class Recorder:
    """Stand-in for send_alert that can be told to fail its first N attempts.

    `attempts` records every call, `messages` only the ones that succeeded, so
    a test can distinguish "was never tried" from "tried and failed".
    """

    def __init__(self, fail_first=0):
        self.attempts = []
        self.messages = []
        self.fail_first = fail_first

    def __call__(self, message):
        self.attempts.append(message)
        if len(self.attempts) <= self.fail_first:
            raise RuntimeError(f"Telegram API 500: simulated outage ({message})")
        self.messages.append(message)
        return len(self.messages)


# ── config helpers ───────────────────────────────────────────────────

def config_for(tickers=("A",), alert=None, **overrides):
    kwargs = dict(poll_interval_seconds=120, max_stored_samples=50,
                  markets=markets_from_tickers(list(tickers), alert or MarketAlert()))
    kwargs.update(overrides)
    return PipelineConfig(**kwargs)


def stdev_alert(threshold=2.5, sample_size=20):
    return MarketAlert(type=AlertType.STDEV, threshold=threshold,
                       sample_size=sample_size)


@pytest.fixture
def storage():
    return FakeStorage()


@pytest.fixture
def recorder():
    return Recorder()
