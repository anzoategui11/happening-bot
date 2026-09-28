# AGENTS.md

Notes for coding agents working in this repo. Read this before editing.

## What this project is

`happening-bot` polls Kalshi prediction markets on an interval, stores recent
prices, and sends a Telegram alert when a market moves unexpectedly.

It is still a **skeleton**. `config.py` builds a `PipelineConfig` and
`src/telegram.py` can send a message, but no module reads the config or calls
`send_alert` — there is no `pipeline.py` and `dags/` is empty. Don't assume a
function is wired up just because it exists and looks finished.

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
```

That is the **only** runnable entry point right now. There is no test suite, no
pytest, and no `test_*.py` files — they have all been deleted. Anything that
talks to Postgres or Telegram has to be driven ad hoc.

Python 3.12.6, deps in `.venv`, managed with `uv`.

## Layout

`src/` is **not a package** — no `__init__.py`. Scripts do
`sys.path.append("src")` *before* importing, because `src/telegram.py` would
otherwise shadow the real `telegram` package. Keep that ordering intact.

| File | Role |
| --- | --- |
| `config.py` | Frozen dataclasses; markets, thresholds, poll interval |
| `src/kalshi_client.py` | Signed REST client, price-field normalisation |
| `src/storage.py` | Postgres writes plus per-ticker pruning |
| `src/alerts.py` | `check_pct` / `check_stdev` — pure threshold logic |
| `src/telegram.py` | `send_alert`, `discover_chat_id` |
| `check_keys.py` | Diagnostic for the Kalshi credential pair |

`src/alerts.py` takes a `storage` object instead of importing one. That
injection point is deliberate — it is the seam tests should use, so write
fakes rather than reaching for a database.

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

- **No tests exist.** Zero, on any branch. `test_telegram.py` and
  `test_storage.py` were both deleted; `.gitignore` still lists
  `test_storage.py`, which is now a dead entry.
- **`Dockerfile` and `docker-compose.yaml` are both 0 bytes**, though
  `.gitignore` carries Airflow entries and `requirements.txt` pulls
  `psycopg2`. The container setup is unwritten.
- **No DB schema is committed.** `storage.py` expects a `market_ticks` table
  with `id`, `ticker`, `price`, and `ts` columns, created by hand.
- **`src/telegram.py` has no caller and no setup docs.** Both functions are
  dead code until a pipeline lands, and the BotFather walkthrough that used to
  live in `test_telegram.py` went with it. `discover_chat_id` is the only way
  to recover your `chat_id` now.
- **Twilio/WhatsApp vars are configured but unused.** No `src/whatsapp.py`
  exists — only a stale `src/__pycache__/whatsapp.*.pyc` from a deleted
  module. Alerts are Telegram-only.
- **`check_keys.py` reaches into `KalshiClient._headers` and `_load_pem`**, both
  private. Fine for a diagnostic, but it's a sign these should be public if a
  second caller ever needs them.

## Working notes

- Git stores LF; the working tree is CRLF on Windows, so `git diff --stat` can
  look noisier than the real change. Judge by the diff, not the stat line.
- `test_storage.py` and `check_keys.py` may exist in your working tree without
  being tracked, depending on which branch you last touched. `check_keys.py` is
  committed on `main`; the storage script is not.
