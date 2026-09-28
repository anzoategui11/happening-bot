"""
Airflow DAG for the happening-bot poll loop.

This schedules poll_once(), NOT run_forever(). The two are alternatives, not
layers: this DAG owns the schedule, so the process must not also sleep in a
loop. Running both would insert every tick twice.

Signal handling is deliberately absent. A signal disposition is process-wide
and last-writer-wins, so registering a handler inside a task would overwrite
whatever the worker installed and break Airflow's own shutdown sequencing.
Airflow owns SIGTERM; this file leaves it alone.

Airflow parses this file on every scheduler sync, in a separate process, so
nothing at module level may touch the network, the database, or the
environment. Everything expensive happens inside the task.
"""

import logging
import os
import sys
from datetime import datetime, timedelta

from airflow.decorators import dag, task

log = logging.getLogger(__name__)

# ── import the bot without a package install ──────────────────────────
# src/ is not on sys.path when the scheduler parses this file, and
# src/telegram.py would shadow a real `telegram` package if the project root
# were added instead -- which is why the append order matters and why
# `insert` must not be used here.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(_PROJECT_ROOT, "src"))
sys.path.append(_PROJECT_ROOT)

from config import CONFIG          # noqa: E402
from kalshi_client import KalshiClient   # noqa: E402
from pipeline import Pipeline      # noqa: E402
from storage import Storage        # noqa: E402

# .env fills gaps only; real environment variables take precedence, so the same
# DAG works locally and in a container that injects these.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    log.warning("python-dotenv not installed; relying on environment variables")


@task
def poll_markets():
    """One poll cycle. Returns the alerts sent, which shows up as an XCom."""
    kalshi = KalshiClient(
        os.environ["KALSHI_KEY_ID"],
        os.environ["KALSHI_PRIVATE_KEY_PATH"],
    )
    # max_stored_samples must come from config or the retention window the
    # alerts are validated against is not the one Storage applies.
    storage = Storage(
        os.environ["DATABASE_URL"],
        max_samples=CONFIG.max_stored_samples,
    )

    sent = Pipeline(kalshi, storage, CONFIG).poll_once()
    log.info("polled %d markets, %d alert(s)", len(CONFIG.markets), len(sent))
    return sent


@dag(
    dag_id="happening_bot",
    # Every 2 minutes, matching CONFIG.poll_interval_seconds. Airflow adds
    # jitter to cron schedules, so real spacing will be 2m plus a little.
    schedule="*/2 * * * *",
    start_date=datetime(2026, 9, 28),
    catchup=False,        # never backfill: a backfill would re-poll old intervals
    max_active_runs=1,    # no overlap, or two runs insert the same window twice
    default_args={
        # Deliberately zero. A retry would re-poll immediately, storing a second
        # tick seconds after the first and skewing the lookback baseline. The next
        # scheduled run is the correct recovery, not an instant re-poll.
        "retries": 0,
    },
    tags=["trading", "alerts"],
    doc_md=__doc__,
)
def happening_bot():
    poll_markets()


happen = happening_bot()
