"""
Entry point for the polling loop.

    uv run python main.py                      # run until interrupted
    uv run python main.py --once               # a single cycle, then exit
    uv run python main.py --backfill [--hours N]   # seed the trade series
    uv run python main.py --prune              # drop rows for removed tickers
    uv run python main.py --stats              # show row counts per ticker
"""

import logging
import os
import sys

sys.path.append("src")  # must come BEFORE importing the src modules

from dotenv import load_dotenv
load_dotenv()

from config import CONFIG
from kalshi_client import KalshiClient
from pipeline import Pipeline
from storage import Storage

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def private_key_path():
    """Resolve KALSHI_PRIVATE_KEY_PATH, which is written relative to the repo.

    Airflow and any container may run this with a working directory of its own
    choosing, where a bare filename would not resolve. Same logic as the DAG.
    """
    path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    if os.path.isabs(path):
        return path
    resolved = os.path.join(PROJECT_ROOT, path)
    if not os.path.exists(resolved):
        raise FileNotFoundError(
            f"KALSHI_PRIVATE_KEY_PATH={path!r} not found at {resolved}. "
            "It is resolved relative to the project root, not the cwd."
        )
    return resolved


def build():
    """Wire up the Kalshi client, storage, and pipeline from the environment."""
    kalshi = KalshiClient(os.environ["KALSHI_KEY_ID"], private_key_path())
    # Both retention caps come from config, otherwise the windows the alerts were
    # validated against are not the ones Storage actually applies.
    storage = Storage(
        os.environ["DATABASE_URL"],
        max_samples=CONFIG.max_stored_samples,
        max_trades=CONFIG.max_stored_trades,
    )
    return Pipeline(kalshi, storage, CONFIG)


def main():
    """Dispatch on the CLI flags and exit. See the module docstring."""
    logging.basicConfig(
        level=logging.DEBUG if "--verbose" in sys.argv else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("happening-bot")

    pipeline = build()

    if "--backfill" in sys.argv:
        # Seed the trade series from Kalshi history. Idempotent, so this is safe
        # to re-run and safe to leave to the scheduler once it has been done.
        hours = 24
        if "--hours" in sys.argv:
            hours = int(sys.argv[sys.argv.index("--hours") + 1])
        added = pipeline.backfill_trades(hours=hours)
        log.info("backfill complete (last %dh): %s", hours,
                 ", ".join(f"{t}={n}" for t, n in added.items()))
        return

    if "--prune" in sys.argv:
        # Per-ticker retention only runs on insert, so a market dropped from the
        # config leaves its rows behind forever. Clean them out.
        active = [m.ticker for m in CONFIG.markets]
        removed = pipeline.storage.prune_orphans(active)
        log.info("pruned orphans outside %d active ticker(s): %s", len(active), removed)
        return

    if "--stats" in sys.argv:
        s = pipeline.storage.stats()
        for table in ("market_ticks", "market_trades"):
            cap = CONFIG.max_stored_samples if table == "market_ticks" else CONFIG.max_stored_trades
            log.info("%s (cap %d per ticker):", table, cap)
            for ticker, n in sorted(s[table].items()):
                flag = "  ORPHAN" if ticker not in [m.ticker for m in CONFIG.markets] else ""
                log.info("    %-34s %5d%s", ticker, n, flag)
        log.info("totals: %s", s["totals"])
        return

    if "--once" in sys.argv:
        sent = pipeline.poll_once()
        log.info("cycle complete, %d alert(s) sent", len(sent))
        return

    try:
        pipeline.run_forever()
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
