import psycopg2

class Storage:
    def __init__(self, dsn, max_samples=50):
        self.dsn = dsn
        self.conn = psycopg2.connect(dsn)
        self.max_samples = max_samples

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