import psycopg2

class Storage:
    def __init__(self, dsn, max_samples=50, max_trades=500):
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.max_samples = max_samples
        self.max_trades = max_trades

    def reconnect(self):
        """Re-establish the connection.

        DATABASE_URL points at Supabase's *transaction* pooler (port 6543),
        which is free to sever idle connections. A long-running poller that
        sits idle between polls will eventually get OperationalError, and the
        only recovery is a fresh connection.
        """
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = psycopg2.connect(self.dsn)

    def insert_tick(self, ticker, price):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO market_ticks (ticker, price) VALUES (%s, %s)",
                (ticker, price))
            # prune: keep only the most recent N rows per ticker
            cur.execute("""
                DELETE FROM market_ticks
                WHERE ticker = %s AND id NOT IN (
                    SELECT id FROM market_ticks
                    WHERE ticker = %s ORDER BY ts DESC LIMIT %s)
            """, (ticker, ticker, self.max_samples))
        self.conn.commit()

    def get_recent(self, ticker, n):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT price FROM market_ticks WHERE ticker=%s ORDER BY ts DESC LIMIT %s",
                (ticker, n))
            return [r[0] for r in cur.fetchall()]

    # ── trade series ────────────────────────────────────────────────
    # One row per executed trade, at the trade's own timestamp. Kept apart
    # from market_ticks because a poll and a trade are different observations:
    # see schema.sql.

    def insert_trades(self, ticker, trades):
        """Store trades, ignoring any already seen. Returns rows actually added.

        ON CONFLICT on trade_id makes this idempotent, so re-fetching the same
        window is free and the poller can overlap the backfill safely.
        """
        if not trades:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO market_trades (trade_id, ticker, price, ts)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (trade_id) DO NOTHING""",
                [(t["trade_id"], ticker, t["price"], t["ts"]) for t in trades])
            added = cur.rowcount
            cur.execute("""
                DELETE FROM market_trades
                WHERE ticker = %s AND id NOT IN (
                    SELECT id FROM market_trades
                    WHERE ticker = %s ORDER BY ts DESC LIMIT %s)
            """, (ticker, ticker, self.max_trades))
        self.conn.commit()
        return added

    def get_recent_trades(self, ticker, n):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT price FROM market_trades WHERE ticker=%s ORDER BY ts DESC LIMIT %s",
                (ticker, n))
            return [r[0] for r in cur.fetchall()]

    def count_trades(self, ticker):
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM market_trades WHERE ticker=%s", (ticker,))
            return cur.fetchone()[0]
