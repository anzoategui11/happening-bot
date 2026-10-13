# happening-bot

Polls [Kalshi](https://kalshi.com) prediction markets on an interval, stores the
prices, and sends a Telegram alert when a market moves more than it reasonably
should.

It tracks a small, hand-picked list of markets (see `config.py`). Every cycle it
reads each market's last traded price, records a sample, and asks two questions:
did the price move a lot recently, and is the latest trade far from its recent
baseline? A "yes" to either sends a message.

## How it works

```
Kalshi REST API ──► KalshiClient ──► Pipeline.poll_once() ──► Storage (Postgres)
                                          │                         │
                                          └──► alerts.check_* ◄─────┘
                                                    │
                                                    ▼
                                              Telegram
```

`Pipeline.poll_once()` is the whole cycle and has no scheduler coupling, so it
can be called from a script, a test, or an Airflow task. `run_forever()` is the
standalone convenience wrapper around it.

### Two series, on purpose

The data is stored in two tables, because a poll and a trade are not the same
kind of observation:

| Table | What it holds | Used for |
| --- | --- | --- |
| `market_ticks` | A 2-minute poll of the last traded price | Percentage-move alerts |
| `market_trades` | One row per executed trade, at the trade's own timestamp | Standard-deviation alerts |

A poll of the last price repeats a stale value between trades, so its variance
measures *polling frequency*, not the market — a market that trades hourly looks
perfectly flat to a 2-minute poll. Trades are sparse but every point is a real
observation, which is what a volatility estimate needs. Gaps are left as gaps;
nothing is padded or carried forward.

### Alert types

Set per market in `config.py`:

- **`pct`** — alerts when the price has moved `threshold` percent or more over
  the last `lookback` polls. Compares `prices[0]` (now) to `prices[-1]` (then).
- **`stdev`** — alerts when the newest trade is `threshold` standard deviations
  from the mean of the previous `sample_size` trades (a z-score).

Each ticker has a cooldown (`alert_cooldown_seconds`, default 2h), so a sustained
move alerts once rather than on every poll. A failed Telegram delivery does *not*
start the cooldown — the alert is retried next cycle.

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv pip install -r requirements.txt

# Apply schema.sql to your Postgres (Supabase) database by hand, then:
uv run python main.py --once      # one live poll cycle
uv run python main.py             # poll forever (Ctrl-C to stop)
```

### Commands

Run from the project root — the scripts rely on relative paths.

```bash
uv run python main.py --once                  # a single cycle, then exit
uv run python main.py                         # run until interrupted
uv run python main.py --backfill [--hours N]  # seed the trade series (default 24h)
uv run python main.py --stats                 # row counts per ticker
uv run python main.py --prune                 # drop rows for tickers not in config
uv run python main.py --verbose               # debug logging
uv run python check_keys.py                   # verify the Kalshi key ID + PEM pair
uv run python check_keys.py other.pem         # check a candidate key file
uv run python -m pytest                       # the test suite
```

`--stats` flags tickers that are no longer in `config.py` as `ORPHAN`, and
`--prune` removes them. Per-ticker retention only runs on insert, so a market
dropped from the config would otherwise leave its rows behind forever.

## Configuration

Everything tunable lives in `config.py`, which is edited directly. Markets,
thresholds, and the poll interval are defined there, and the file validates its
own constraints at import (for example, a stdev market needs
`sample_size >= 3`, and `max_stored_samples` must be large enough for every
market's window).

```python
TICKERS = [
    "KXMACHADOVENEZUELA-26JUN29-OCT01",
    # ...
]

CONFIG = PipelineConfig(
    poll_interval_seconds=120,      # 2 min
    max_stored_samples=50,          # polled rows kept per ticker
    markets=markets_from_tickers(TICKERS),
    alert_cooldown_seconds=7200,    # 2h
    max_stored_trades=500,          # trade rows kept per ticker
)
```

To give specific tickers a different alert type or threshold, build them
separately and combine them:

```python
markets=(
    markets_from_tickers(TICKERS[:-1])
    + markets_from_tickers(TICKERS[-1:], MarketAlert(type=AlertType.STDEV, threshold=2.5))
)
```

A market must have traded at least once to be tracked — the pipeline refuses to
track a market with no `last_price`.

## Environment

Copy the variables into a `.env` file at the repo root (gitignored, never
commit it). The bot reads them via `python-dotenv`.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Supabase Postgres connection string |
| `KALSHI_KEY_ID` | Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | Path to the Kalshi PEM, resolved relative to the repo root |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Destination chat |
| `AIRFLOW_FERNET_KEY` | Required by the Airflow deployment (see below) |

To find `TELEGRAM_CHAT_ID`: message your bot, then run
`discover_chat_id` from `src/telegram.py` and copy the printed value.

The Kalshi PEM is a live trading credential and is gitignored along with `.env`.
If requests start returning `401 INCORRECT_API_KEY_SIGNATURE`, run
`check_keys.py` — it distinguishes a corrupt PEM from a key ID that doesn't
match the PEM, which Kalshi itself does not.

## Scheduling with Airflow

`dags/happening_bot.py` schedules `poll_once()` every 2 minutes. The DAG is
deliberately *not* the same as `run_forever()` — Airflow owns the schedule, so
the process must not also sleep in a loop, or every tick would be stored twice.
Retries are set to 0 for the same reason: a retry would re-poll immediately and
store a second tick seconds after the first.

`docker-compose.yaml` brings up Airflow with a dedicated metadata Postgres (kept
separate from the Supabase price database):

```bash
docker compose up -d --build
docker compose run --rm airflow-init      # one-time: create/migrate metadata DB
# UI at http://localhost:8080  (airflow / airflow)
docker compose exec airflow-scheduler airflow dags unpause happening_bot
```

The project is bind-mounted, not baked into the image, so editing `dags/` or
`src/` needs only a restart. The DAG ships **paused** on purpose — unpausing is
a conscious action.

The `standalone` compose profile runs `python main.py` for comparison against the
DAG. Never run both at once.

## Tests

```bash
uv pip install -r requirements-dev.txt
uv run python -m pytest
```

The suite is hermetic by construction. The `no_real_io` autouse fixture in
`tests/conftest.py` blocks `psycopg2.connect` and `requests`, so a test that
reaches for a real `Storage` or `KalshiClient` fails loudly instead of writing
to live Supabase or hitting Kalshi. Everything runs against the fakes in
`conftest.py`. A test that needs the network belongs in a manual script, not
here.

`tests/test_suite_guarantees.py` is meta: it asserts the suite can still go red
by reintroducing historical bugs.

## Layout

`src/` is **not** a package (no `__init__.py`). Scripts do
`sys.path.append("src")` *before* importing, because `src/telegram.py` would
otherwise shadow the real `telegram` package.

| File | Role |
| --- | --- |
| `config.py` | Frozen dataclasses; markets, thresholds, poll interval |
| `main.py` | Entry point: `--once`, `--backfill`, `--stats`, `--prune` |
| `src/pipeline.py` | `poll_once` / `run_forever`; dispatch, cooldown, error policy |
| `src/kalshi_client.py` | Signed REST client, price-field normalisation |
| `src/storage.py` | Postgres writes, per-ticker pruning, orphan cleanup |
| `src/alerts.py` | `check_pct` / `check_stdev` — pure threshold logic |
| `src/telegram.py` | `send_alert`, `discover_chat_id` |
| `dags/happening_bot.py` | Airflow DAG, TaskFlow, calls `poll_once()` only |
| `check_keys.py` | Diagnostic for the Kalshi credential pair |
| `schema.sql` | Both series; applied by hand, `IF NOT EXISTS` |
| `tests/` | Pytest suite; hermetic, see above |

See `AGENTS.md` for the invariants that are load-bearing and the known gaps.
