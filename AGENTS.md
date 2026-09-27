# AGENTS.md

Notes for coding agents working in this repo. Read this before editing.

## What this project is

`happening-bot` polls Kalshi prediction markets on an interval, stores recent
prices, and sends a Telegram alert when a market moves unexpectedly.

It is a **skeleton**: the pieces exist, but nothing wires them together yet.
There is no `pipeline.py`, and `dags/` is empty. `config.py` builds a
`PipelineConfig` that no module currently reads. Before adding features, check
whether the missing piece is intentional.

## Commands

Run everything from the project root — the scripts rely on relative paths.

```bash
uv run python check_keys.py        # verify Kalshi key ID + PEM actually pair
uv run python test_telegram.py     # send a real test message
uv run python test_telegram.py --discover   # print your chat_id
uv run python test_storage.py      # hits the real database; see caveat below
```

There is **no test suite and no pytest**. The `test_*.py` files are manual
scripts with top-level `assert`s that talk to live services. Don't try to
`pytest` them, and don't assume a green run means anything offline.

Python 3.12.6, deps in `.venv`, managed with `uv`.

## Layout

`src/` is **not a package** — no `__init__.py`. Every script does
`sys.path.append("src")` *before* importing, because `src/telegram.py` would
otherwise shadow the real `telegram` package. Keep that ordering intact.

| File | Role |
| --- | --- |
| `config.py` | Frozen dataclasses; markets, thresholds, poll interval |
| `src/kalshi_client.py` | Signed REST client, `_load_pem` helper |
| `src/storage.py` | Postgres writes plus per-ticker pruning |
| `src/alerts.py` | `check_pct` / `check_stdev` — pure threshold logic |
| `src/telegram.py` | `send_alert`, `discover_chat_id` |

`src/alerts.py` takes a `storage` object and reads from it rather than
importing one, which is what makes it testable in isolation. Preserve that.

## Invariants worth defending

- **Storage and alerts assume `get_recent()` returns newest-first.** It does,
  via `ORDER BY ts DESC`. `check_pct` treats `prices[0]` as current and
  `prices[-1]` as the comparison point; reversing the order silently inverts
  every alert instead of raising.
- **A sample is only "ready" once `len(prices)` is past the window.** Both
  checks return `None` early otherwise. Alerting on a partial window is the
  bug this guards against.
- **`check_stdev` needs `sample_size >= 3` to do anything.** It excludes the
  newest tick from the baseline, so the baseline is `prices[1:]` and
  `statistics.stdev` needs at least two of its own points; with a smaller
  window it returns `None` rather than raising. Note `config.py` still only
  rejects `sample_size < 2`, so a market configured with `2` silently never
  fires.
- **`config.py` validates its own constraints at import.** It rejects duplicate
  tickers, more than 20 markets, and any market whose `lookback` or
  `sample_size` exceeds `max_stored_samples`. If you add a market, raise
  `max_stored_samples` rather than loosening the check.
- **`KalshiClient._prefix` is derived from `base_url`** so the signed path can
  never drift from the URL actually requested. Don't "simplify" it into a
  constant. The query string is deliberately excluded from the signed message.
- **`_load_pem` exists for a real reason:** the Kalshi dashboard serves the
  whole key as one line, which OpenSSL refuses to parse. It re-wraps at 64
  columns first. Don't remove it as redundant.
- **Telegram caps a message at 4096 chars** and `send_alert` raises rather
  than silently truncating. Keep that a hard error.

## Secrets

`.env` and `kalshi_private_key.pem` are gitignored and must stay that way.
`.env` holds `DATABASE_URL` (Supabase Postgres), `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`, and a Twilio
set (`TWILIO_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`,
`ALERT_WHATSAPP_TO`).

Never print, log, or commit their values — reference them by name only. The
private key is a real trading credential; `kalshi_private_key.pem` is
deliberately untracked.

## Known gaps

Fix these if you touch the surrounding code, or at least don't build on top
of them as if they were handled.

- **`requirements.txt` is missing `python-dotenv`**, which both scripts
  import. It only resolves because it's already in `.venv`; a clean install
  breaks.
- **`test_storage.py` is listed in `.gitignore`.** A test that only exists on
  one machine is not a test. It should be un-ignored.
- **`Dockerfile` and `docker-compose.yaml` are both 0 bytes**, despite
  `.gitignore` carrying Airflow entries and `requirements.txt` pulling
  `psycopg2`. The container setup is unwritten.
- **No DB schema is committed.** `storage.py` expects a `market_ticks` table
  with `id`, `ticker`, `price`, and `ts` columns. `test_storage.py` assumes
  you created it in Supabase by hand.
- **Twilio/WhatsApp vars are configured but unused.** There's no
  `src/whatsapp.py` — only a stale `src/__pycache__/whatsapp.*.pyc` from a
  deleted module. Alerts are Telegram-only today.
- **`test_storage.py` needs a live `DATABASE_URL`** and inserts rows under
  `TESTTICKER` / `ALERTTEST` / `STDEVTEST`. It is not safe to run blind
  against a real database.

## Working notes

- Git stores LF; the working tree is CRLF on Windows. Expect noisy
  `git diff --stat` right after writes. As of writing, `config.py`,
  `src/alerts.py`, and `src/kalshi_client.py` carry uncommitted changes and
  `check_keys.py` is untracked — that work is in flight, so diff before
  assuming a line is committed behaviour.
- `check_keys.py` reaches into `KalshiClient._headers` and `_load_pem`, both
  private. That's accepted for a diagnostic script, but it's a signal that
  these could be public if a second caller ever needs them.
