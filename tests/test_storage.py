"""
storage.py against a real Postgres.

Everything else in the suite is hermetic because asserting a DELETE against a
mock cursor tests the mock rather than the query. These tests are not: they
need a real database, because the behaviour that matters here -- retention
pruning, newest-first ordering, ON CONFLICT dedupe -- *is* the SQL.

They run against a throwaway database, never the Supabase instance that holds
real market data, and the module refuses to run against anything else.

Requirements:
    docker compose up -d postgres
    # or set STORAGE_TEST_DSN to your own throwaway database

Skipped automatically when no such database is reachable, so the main suite
stays runnable with no Docker at all.
"""

import os

import pytest
import psycopg2

pytestmark = pytest.mark.allow_db

# Never defaults to DATABASE_URL. That variable points at real market data and
# these tests create and drop tables.
DEFAULT_DSN = "postgresql://airflow:airflow@localhost:5433/happening_bot_test"
REQUIRED_DB_NAME = "happening_bot_test"

DDL = """
CREATE TABLE IF NOT EXISTS market_ticks (
    id     BIGSERIAL PRIMARY KEY,
    ticker TEXT        NOT NULL,
    price  NUMERIC     NOT NULL,
    ts     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS market_ticks_ticker_ts_idx ON market_ticks (ticker, ts DESC);

CREATE TABLE IF NOT EXISTS market_trades (
    id       BIGSERIAL PRIMARY KEY,
    trade_id TEXT        NOT NULL UNIQUE,
    ticker   TEXT        NOT NULL,
    price    NUMERIC     NOT NULL,
    ts       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS market_trades_ticker_ts_idx ON market_trades (ticker, ts DESC);
"""


def _redact(dsn):
    return dsn.split("@")[-1] if "@" in dsn else dsn


def _dsn():
    dsn = os.environ.get("STORAGE_TEST_DSN", DEFAULT_DSN)
    db = dsn.rsplit("/", 1)[-1].split("?")[0]
    if db != REQUIRED_DB_NAME:
        # A misconfiguration must be loud, never a skip.
        raise pytest.UsageError(
            f"STORAGE_TEST_DSN points at database {db!r}, but these tests DROP and "
            f"CREATE tables and must only ever run against {REQUIRED_DB_NAME!r}.\n"
            "Set STORAGE_TEST_DSN to a throwaway database, e.g.\n"
            f"  {DEFAULT_DSN}\n"
            "and never to DATABASE_URL, which holds real market data."
        )
    return dsn


def _reachable(dsn):
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


_DSN = _dsn()
if not _reachable(_DSN):
    pytest.skip(
        f"no throwaway Postgres at {_redact(_DSN)}. Start one with "
        "'docker compose up -d postgres', or set STORAGE_TEST_DSN.",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def dsn():
    return _DSN


@pytest.fixture(scope="module", autouse=True)
def schema(dsn):
    """Create the tables once for the module."""
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.close()
    yield


@pytest.fixture
def storage(dsn):
    """A Storage over empty tables.

    The teardown matters: get_recent() runs a SELECT and never commits, so the
    connection is left idle-in-transaction holding an ACCESS SHARE lock. Left
    open, the next test's TRUNCATE (which wants ACCESS EXCLUSIVE) blocks on it
    forever. Roll back and close before yielding control back.
    """
    from storage import Storage
    s = Storage(dsn, max_samples=5, max_trades=20)

    with s.conn.cursor() as cur:
        cur.execute("TRUNCATE market_ticks, market_trades RESTART IDENTITY")
    s.conn.commit()

    yield s

    try:
        s.conn.rollback()
    finally:
        s.conn.close()


def rows(storage, table):
    with storage.conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]


def approx(values):
    """Compare against Decimals, because that is what a numeric column returns.

    pytest.approx on a float literal would do `float - Decimal` and raise
    TypeError rather than simply failing the comparison.
    """
    from decimal import Decimal
    if not isinstance(values, (list, tuple)):
        values = [values]
    return pytest.approx([Decimal(str(v)) for v in values])


# ── the polled series ────────────────────────────────────────────────

class TestInsertTick:
    def test_stores_a_row(self, storage):
        storage.insert_tick("A", 0.5)
        assert rows(storage, "market_ticks") == 1

    def test_round_trips_the_price(self, storage):
        storage.insert_tick("A", 0.58)
        assert storage.get_recent("A", 1) == approx([0.58])

    def test_returns_newest_first(self, storage):
        for p in (0.1, 0.2, 0.3):
            storage.insert_tick("A", p)
        assert storage.get_recent("A", 10) == approx([0.3, 0.2, 0.1])

    def test_respects_the_limit_argument(self, storage):
        for p in (0.1, 0.2, 0.3):
            storage.insert_tick("A", p)
        assert storage.get_recent("A", 2) == approx([0.3, 0.2])

    def test_unknown_ticker_is_empty_not_an_error(self, storage):
        assert storage.get_recent("NOPE", 5) == []


class TestRetention:
    def test_prunes_to_max_samples(self, storage):
        for p in range(8):
            storage.insert_tick("A", p)
        assert rows(storage, "market_ticks") == 5

    def test_keeps_the_newest_on_prune(self, storage):
        for p in range(8):
            storage.insert_tick("A", p)
        assert storage.get_recent("A", 10) == approx([7, 6, 5, 4, 3])

    def test_pruning_one_ticker_leaves_others_alone(self, storage):
        for p in range(8):
            storage.insert_tick("A", p)
        for p in range(2):
            storage.insert_tick("B", p)
        assert storage.get_recent("A", 10) == approx([7, 6, 5, 4, 3])
        assert storage.get_recent("B", 10) == approx([1, 0])

    def test_no_duplicate_tickers_within_a_ticker(self, storage):
        for p in range(20):
            storage.insert_tick("A", p)
        assert len(storage.get_recent("A", 100)) == 5


# ── the trade series ─────────────────────────────────────────────────

class TestTrades:
    def test_stores_a_trade(self, storage):
        storage.insert_trades("A", [{"trade_id": "t1", "ts": _now(0), "price": 0.5}])
        assert rows(storage, "market_trades") == 1

    def test_ignores_a_duplicate_trade_id(self, storage):
        t = [{"trade_id": "t1", "ts": _now(0), "price": 0.5}]
        storage.insert_trades("A", t)
        added = storage.insert_trades("A", t)
        assert added == 0
        assert rows(storage, "market_trades") == 1

    def test_reports_how_many_were_new(self, storage):
        added = storage.insert_trades("A", [
            {"trade_id": "t1", "ts": _now(0), "price": 0.5},
            {"trade_id": "t2", "ts": _now(60), "price": 0.6},
        ])
        assert added == 2

    def test_empty_list_is_a_no_op(self, storage):
        assert storage.insert_trades("A", []) == 0

    def test_orders_by_timestamp_not_insertion_order(self, storage):
        """The point of storing Kalshi's own timestamps: trades are inserted in
        whatever order a poll happens to see them, and the order that matters
        is chronological, not insertion order.

        _now(0) is the most recent; _now(600) is ten minutes older.
        """
        storage.insert_trades("A", [
            {"trade_id": "ten-minutes-ago", "ts": _now(600), "price": 0.9},
            {"trade_id": "just-now", "ts": _now(0), "price": 0.1},
        ])
        assert storage.get_recent_trades("A", 10) == approx([0.1, 0.9])

    def test_prunes_to_max_trades(self, storage):
        for i in range(25):
            storage.insert_trades("A", [{"trade_id": f"t{i}", "ts": _now(i * 60),
                                         "price": i / 100}])
        assert rows(storage, "market_trades") == 20

    def test_keeps_the_newest_trades_on_prune(self, storage):
        # _now(i * 60) counts backwards, so i=0 is the newest trade
        for i in range(25):
            storage.insert_trades("A", [{"trade_id": f"t{i}", "ts": _now(i * 60),
                                         "price": i / 100}])
        assert storage.get_recent_trades("A", 3) == approx([0.0, 0.01, 0.02])

    def test_count_trades(self, storage):
        storage.insert_trades("A", [
            {"trade_id": "a", "ts": _now(0), "price": 0.1},
            {"trade_id": "b", "ts": _now(60), "price": 0.2},
        ])
        assert storage.count_trades("A") == 2


# ── database cap ─────────────────────────────────────────────────────

class TestPruneOrphans:
    def test_removes_tickers_outside_the_config(self, storage):
        for t in ("KEEP", "DROP1", "DROP2"):
            storage.insert_tick(t, 0.5)
        removed = storage.prune_orphans(["KEEP"])
        assert removed["market_ticks"] == 2
        assert storage.get_recent("KEEP", 5)
        assert storage.get_recent("DROP1", 5) == []

    def test_clears_both_series(self, storage):
        storage.insert_tick("DROP", 0.5)
        storage.insert_trades("DROP", [{"trade_id": "x", "ts": _now(0), "price": 0.5}])
        removed = storage.prune_orphans(["KEEP"])
        assert removed == {"market_ticks": 1, "market_trades": 1}

    def test_empty_config_clears_everything(self, storage):
        storage.insert_tick("A", 0.5)
        storage.insert_trades("A", [{"trade_id": "x", "ts": _now(0), "price": 0.5}])
        storage.prune_orphans([])
        assert rows(storage, "market_ticks") == 0
        assert rows(storage, "market_trades") == 0

    def test_leaves_active_tickers_untouched(self, storage):
        for p in range(3):
            storage.insert_tick("KEEP", p)
        storage.prune_orphans(["KEEP"])
        assert storage.get_recent("KEEP", 10) == approx([2, 1, 0])


class TestStats:
    def test_counts_per_ticker(self, storage):
        storage.insert_tick("A", 0.5)
        storage.insert_tick("A", 0.6)
        storage.insert_tick("B", 0.1)
        s = storage.stats()
        assert s["market_ticks"] == {"A": 2, "B": 1}

    def test_includes_totals(self, storage):
        storage.insert_tick("A", 0.5)
        storage.insert_trades("A", [{"trade_id": "x", "ts": _now(0), "price": 0.5}])
        s = storage.stats()
        assert s["totals"] == {"market_ticks": 1, "market_trades": 1}


class TestReconnect:
    def test_keeps_working_after_a_reconnect(self, storage):
        storage.insert_tick("A", 0.5)
        storage.reconnect()
        storage.insert_tick("A", 0.6)
        assert storage.get_recent("A", 5) == approx([0.6, 0.5])

    def test_survives_a_dead_connection(self, storage):
        """The transaction pooler severs idle connections; this is the case
        that motivated reconnect() in the first place."""
        storage.insert_tick("A", 0.5)
        storage.conn.close()
        storage.reconnect()
        storage.insert_tick("A", 0.6)
        assert len(storage.get_recent("A", 5)) == 2


def _now(offset_seconds):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
