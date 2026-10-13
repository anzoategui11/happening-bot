-- Schema for happening-bot. Applied by hand; see AGENTS.md.
--
-- Two series are kept deliberately separate, because they are not the same
-- kind of observation and mixing them would corrupt both:
--
--   market_ticks  a 2-minute poll of the last traded price. Cheap and current,
--                 but repeats the same value between trades, so its variance
--                 measures polling frequency rather than the market.
--   market_trades one row per executed trade, at the trade's own timestamp.
--                 Sparse and irregular, but every point is a real observation,
--                 so it is the only valid basis for a volatility estimate.
--
-- Nothing is ever padded or carried forward. Gaps are real information.

CREATE TABLE IF NOT EXISTS market_ticks (
    id     BIGSERIAL PRIMARY KEY,
    ticker TEXT        NOT NULL,                 -- Kalshi market ticker
    price  NUMERIC     NOT NULL,                 -- last traded price, 0.00-1.00
    ts     TIMESTAMPTZ NOT NULL DEFAULT now()    -- when the poll happened
);

CREATE INDEX IF NOT EXISTS market_ticks_ticker_ts_idx
    ON market_ticks (ticker, ts DESC);

-- trade_id is Kalshi's own UUID for the trade, which makes re-fetching the
-- same window idempotent without guessing at timestamp collisions.
CREATE TABLE IF NOT EXISTS market_trades (
    id       BIGSERIAL PRIMARY KEY,
    trade_id TEXT        NOT NULL UNIQUE,   -- Kalshi's UUID; makes inserts idempotent
    ticker   TEXT        NOT NULL,
    price    NUMERIC     NOT NULL,          -- executed yes-side price
    ts       TIMESTAMPTZ NOT NULL           -- the trade's own timestamp
);

CREATE INDEX IF NOT EXISTS market_trades_ticker_ts_idx
    ON market_trades (ticker, ts DESC);
