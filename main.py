"""
Entry point for the polling loop.

    uv run python main.py          # run until interrupted
    uv run python main.py --once   # a single cycle, then exit
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


def build():
    kalshi = KalshiClient(
        os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"]
    )
    # max_stored_samples has to come from config, otherwise the retention window
    # the alerts were validated against is not the one Storage actually applies.
    storage = Storage(os.environ["DATABASE_URL"], max_samples=CONFIG.max_stored_samples)
    return Pipeline(kalshi, storage, CONFIG)


def main():
    logging.basicConfig(
        level=logging.DEBUG if "--verbose" in sys.argv else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("happening-bot")

    pipeline = build()

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
