# AGENTS.md

Notes for coding agents working in this repo. Read this before editing.

## What this project is

`happening-bot` polls Kalshi prediction markets on an interval, stores recent
prices, and sends a Telegram alert when a market moves unexpectedly.

The loop is wired up: `src/pipeline.py` polls, `dags/happening_bot.py`
schedules `poll_once()`, and `send_alert` is called for real. It has delivered
a live alert end to end. Two series are kept on purpose — a 2-minute poll for
percentage moves, and executed trades for standard deviation — because
polling a market that trades hourly measures polling frequency, not the market.

## Branches

- `main` — the integration branch. All real work lands here via PR.
- `working` — where changes are developed before opening a PR.
- `unity_tests` — the test-writing branch, fast-forwarded level with `main`.
  It used to be cut from `3ab04e4` and was missing the signing fix, the
  price-field normalisation, the alert crash fixes, and the
  `test_telegram.py` deletion; that is fixed, so it now starts from current
  code. Keep it level with `main` — if `main` moves, merge it in again before
  writing more tests.

## Commands

Run from the project root — the scripts rely on relative paths.

```bash
uv run python check_keys.py            # verify the Kalshi key ID and PEM pair
uv run python check_keys.py other.pem  # check a candidate key file
uv run python main.py --once           # one live poll cycle
uv run python main.py --backfill       # seed the trade series from history
uv run python main.py --stats          # row counts per ticker
uv run python main.py --prune          # drop rows for tickers not in config
uv run python -m pytest                # the test suite
```

## Tests

`tests/` holds a pytest suite, run with `uv run python -m pytest`.

**They are hermetic by construction, and that is the point.** The `no_real_io`
autouse fixture in `tests/conftest.py` blocks `psycopg2.connect` and
`requests`, so a test that reaches for a real `Storage` or `KalshiClient` fails
loudly instead of writing to live Supabase or hitting Kalshi. Everything runs
against the fakes in `conftest.py`. Keep it that way — a test needing the
network belongs in a manual script, not here.

`tests/test_suite_guarantees.py` is meta: it asserts the suite can still go
red, by reintroducing historical bugs. Worth extending when you add behaviour
that was previously unguarded.

pytest is a **dev** dependency (`requirements-dev.txt`); the bot never imports
it. `pytest.ini` sets `pythonpath = . src`, which is why tests import `alerts`
and friends directly instead of repeating `sys.path.append("src")`.

Python 3.12.6, deps in `.venv`, managed with `uv`.

## Layout

`src/` is **not a package** — no `__init__.py`. Scripts do
`sys.path.append("src")` *before* importing, because `src/telegram.py` would
otherwise shadow the real `telegram` package. Keep that ordering intact.

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
| `tests/` | pytest suite; hermetic, see above |

`src/alerts.py` takes a `storage` object instead of importing one. That
injection point is deliberate — it is the seam the tests use, so write fakes
rather than reaching for a database.

## Invariants worth defending

- **`get_recent()` returns newest-first** (`ORDER BY ts DESC`), and the alert
  code depends on it. `check_pct` treats `prices[0]` as current and
  `prices[-1]` as the comparison point; reversing the order silently inverts
  every alert instead of raising.
- **`get_market()` normalises Kalshi's price fields, and callers rely on
  that.** Kalshi moved to dollar-denominated keys and left the old integer-cent
  names in the payload as permanently-null placeholders, so reading
  `last_price` or `yes_bid` off a raw response silently yields `None`.
  `_normalise_market` back-fills the legacy names from the live ones via
  `_PRICE_FIELDS`. Keep both in sync if Kalshi renames fields again, and keep
  `_num`'s `None`/`""` tolerance — it is what turns a null placeholder into a
  usable value instead of a `TypeError`.
- **Misconfiguration and warm-up are different failures in `check_stdev`.** A
  `sample_size < 3` raises immediately, because the newest tick is excluded
  from the baseline and `statistics.stdev` over `prices[1:]` needs two points
  of its own. Too few rows *stored* is normal during warm-up and returns
  `None`. Don't collapse these into one silent `None`.
- **`config.py` validates its own constraints at import**, including
  `sample_size >= 3` for stdev markets. If you add a market, raise
  `max_stored_samples` rather than loosening a check.
- **`KalshiClient._prefix` is derived from `base_url`** so the signed path can
  never drift from the URL actually requested. Don't collapse it into a
  constant. The query string is deliberately excluded from the signed message.
- **`_load_pem` exists for a real reason:** the Kalshi dashboard serves the
  whole key as one line, which OpenSSL refuses to parse. It re-wraps at 64
  columns first. Don't remove it as redundant.
- **Telegram caps a message at 4096 chars** and `send_alert` raises rather
  than truncating. Keep that a hard error.

## Secrets

`.env` and `kalshi_private_key.pem` are gitignored and must stay that way.
`.env` holds `DATABASE_URL` (Supabase Postgres), `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, and a Twilio
set (`TWILIO_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`,
`ALERT_WHATSAPP_TO`).

Never print, log, or commit their values — reference them by name only. The
PEM is a live trading credential.

## Known gaps

- **The DAG ships paused.** Airflow sets `core.dags_are_paused_at_creation`, so
  a fresh stack needs `docker compose exec airflow-scheduler airflow dags
  unpause happening_bot` before the schedule does anything. Deliberately not
  automated: unpausing should be a conscious action.
- **The bot cannot report its own failure.** If the Airflow task starts erroring
  you find out by opening the UI. There is no alert-on-error path.
- **The BotFather setup walkthrough is gone.** It lived in `test_telegram.py`,
  which was deleted. `discover_chat_id` is the only way to recover a `chat_id`.
- **Trade-fetch batching is a no-op under Airflow.** It keeps state in memory
  and each DAG run is a fresh process, so the state never accumulates. It only
  helps `run_forever`. Persisting a watermark would fix it; judged not worth
  one request per market per cycle.
- **Trade history is bounded by the fetch limit.** `get_trades(limit=200)`
  reaches back ~2 days on a 100-trade/day market while `max_stored_trades`
  allows ~5. Ongoing accumulation is fine; a first deep backfill is not, and
  `backfill_trades` warns when it truncates.
- **`depth_warning` thresholds are guesses**, flagged TODO in
  `src/pipeline.py`. `liquidity_dollars` is deliberately unused: Kalshi
  returned `0.0000` for all 60 markets sampled.
- **`src/storage.py` is untested.** Its SQL runs only against the live
  database, since mocking a cursor would test the mock. Retention and orphan
  pruning are covered by `main.py --stats` / `--prune` instead.
- **Twilio/WhatsApp vars are configured but unused.** No `src/whatsapp.py`
  exists — only a stale `src/__pycache__/whatsapp.*.pyc` from a deleted
  module. Alerts are Telegram-only.
- **`check_keys.py` reaches into `KalshiClient._headers` and `_load_pem`**, both
  private. Fine for a diagnostic, but it's a sign these should be public if a
  second caller ever needs them.
- **`.gitignore` still lists `test_storage.py`**, a dead entry — the file was
  deleted long ago.

## Working notes

- Git stores LF; the working tree is CRLF on Windows, so `git diff --stat` can
  look noisier than the real change. Judge by the diff, not the stat line.
- Never rewrite a text file with PowerShell `Set-Content -NoNewline`; it strips
  every newline and flattens the file. Use the edit tool, or `git checkout` to
  recover.
- The suite runs in ~6s because nothing is mocked at the I/O boundary beyond
  two blocked functions. If you find yourself wanting a live integration test,
  it belongs in a manual script.
