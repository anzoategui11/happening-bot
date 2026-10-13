"""Poll loop: read markets, store prices, alert on unusual moves.

poll_once() is deliberately free of any scheduler coupling so it can be called
from a script, from a test, or from an Airflow task. run_forever() is the
standalone convenience wrapper.
"""

import logging
import signal
import time

import psycopg2
import requests

from alerts import check_pct, check_stdev
from config import AlertType
from telegram import send_alert

log = logging.getLogger("happening-bot")


class MarketInactive(RuntimeError):
    """The market is no longer open. The configured ticker is stale."""


class MarketNeverTraded(RuntimeError):
    """The market has never printed a trade, so there is no price to track."""


# Thresholds below are a first pass, not a calibrated model. They were set by
# eye against real output from these four markets, not from a study of order-book
# depth. TODO: decide whether a thin market should warn once, be excluded, or
# just be listed with a note -- and re-tune once more markets are being tracked.
#
# Note: Kalshi returns liquidity_dollars as 0.0000 for every market sampled, so
# it is not usable as a depth signal and is deliberately not checked here.
DEPTH_MIN_VOLUME_24H = 1.0      # contracts traded in the last 24h
DEPTH_MIN_OPEN_INTEREST = 1.0   # contracts held across the book
DEPTH_MIN_BID_SIZE = 1.0        # contracts resting at the top of book

# Force a trade re-fetch at least this often, in cycles, regardless of whether
# the market looks unchanged. 15 cycles at a 2 min interval is ~30 min.
TRADE_REFRESH_EVERY_CYCLES = 15


def depth_warning(market):
    """Describe why a market is too thin to trust, or None if it looks healthy.

    A last_price can be a single trade in a near-empty book, so a market that
    reports one can still be a trap to alert on.
    """
    problems = []
    if (market.get("volume_24h") or 0) < DEPTH_MIN_VOLUME_24H:
        problems.append(f"24h volume {market.get('volume_24h')}")
    if (market.get("open_interest") or 0) < DEPTH_MIN_OPEN_INTEREST:
        problems.append(f"open interest {market.get('open_interest')}")
    bid_size = market.get("yes_bid_size")
    if bid_size is not None and bid_size < DEPTH_MIN_BID_SIZE:
        problems.append(f"top-of-book bid size {bid_size}")
    return ", ".join(problems) if problems else None


class Pipeline:
    """Polls each configured market once per cycle, with cooldown and retries."""

    def __init__(self, kalshi, storage, config, notify=send_alert):
        self.kalshi = kalshi
        self.storage = storage
        self.config = config
        self.notify = notify
        # ticker -> monotonic timestamp of the last message we sent
        self._last_alerted = {}
        self._last_problem = {}
        # ticker -> (last_price, volume) at the last trade fetch, used to skip
        # fetches for a market that has not traded
        self._last_seen = {}
        self._cycles_since_trades = {}
        self._running = True

    # ── one cycle ────────────────────────────────────────────────────

    def poll_once(self):
        """Poll every configured market once. Returns the alerts sent.

        Only *expected* per-market failures are absorbed here. Anything
        unexpected is allowed to propagate: a bug should fail the Airflow task
        loudly rather than be retried silently every two minutes forever.
        """
        sent = []
        for market in self.config.markets:
            try:
                alert = self._poll_market(market)
            except (MarketInactive, MarketNeverTraded) as e:
                # These mean the config is wrong, not that the market is quiet.
                # Surface it once per cooldown rather than every cycle.
                self._report_problem(market.ticker, e)
                continue
            except requests.RequestException as e:
                # Kalshi being unreachable or returning an error. Transient, so
                # skip this market and try again next cycle.
                log.warning("%s: Kalshi request failed: %s", market.ticker, e)
                continue
            except psycopg2.OperationalError:
                log.exception("database connection lost, reconnecting")
                try:
                    self.storage.reconnect()
                except Exception:
                    log.exception("reconnect failed; skipping this cycle")
                continue
            except psycopg2.Error:
                # Non-connection database error: constraint violation, bad SQL,
                # and so on. Retryable in principle, but it is a code or schema
                # problem, so make it visible rather than swallowing it.
                log.exception("%s: database error", market.ticker)
                raise

            if alert and self._in_cooldown(market.ticker):
                log.debug("%s: alert suppressed, still in cooldown", market.ticker)
            elif alert:
                delivered = self._deliver(market.ticker, alert)
                if delivered is not None:
                    sent.append(delivered)
        return sent

    def _poll_market(self, market):
        data = self.kalshi.get_market(market.ticker)
        ticker = market.ticker

        status = data.get("status")
        if status != "active":
            raise MarketInactive(f"{ticker}: status is {status!r}, expected 'active'")

        price = data.get("last_price")
        if price is None or (price == 0 and not data.get("volume")):
            raise MarketNeverTraded(f"{ticker}: no last_price and no volume, never traded")

        thin = depth_warning(data)
        if thin:
            log.warning("%s: thin market, alerts may be unreliable (%s)", ticker, thin)

        # Trades are only re-fetched when the market has actually traded. A
        # trade necessarily moves lifetime volume, so an unchanged
        # (last_price, volume) pair means there is nothing new to collect, and
        # the market fetch above already gave us that pair for free. The
        # periodic refresh is a backstop: volume is reported to 2 decimals, so
        # a very small trade could leave it unchanged.
        signature = (price, data.get("volume"))
        cycles = self._cycles_since_trades.get(ticker, 0) + 1
        if signature != self._last_seen.get(ticker) or cycles >= TRADE_REFRESH_EVERY_CYCLES:
            self.storage.insert_trades(ticker, self.kalshi.get_trades(ticker))
            self._last_seen[ticker] = signature
            self._cycles_since_trades[ticker] = 0
        else:
            self._cycles_since_trades[ticker] = cycles
            log.debug("%s: no trade since last fetch (volume %s), skipping", ticker, signature[1])

        # Insert before checking: check_pct reads prices[0] as the current tick,
        # so the sample under evaluation has to already be in the series.
        self.storage.insert_tick(ticker, price)

        alert = market.alert
        if alert.type == AlertType.PCT:
            return check_pct(self.storage, ticker, alert.threshold, alert.lookback)
        return check_stdev(self.storage, ticker, alert.threshold, alert.sample_size,
                           warn=lambda msg: self._report_problem(ticker + ":stdev", msg))

    # ── sending ──────────────────────────────────────────────────────

    def _in_cooldown(self, ticker):
        """True if this ticker alerted recently. Keeps a sustained move from
        re-alerting on every single poll."""
        last = self._last_alerted.get(ticker)
        if last is None:
            return False
        return time.monotonic() - last < self.config.alert_cooldown_seconds

    def _deliver(self, ticker, message):
        """Send an alert, starting the cooldown only once it has been delivered.

        A delivery failure must not abort the cycle -- the markets after this
        one still need polling, and an alert that never arrived must not be
        silenced for the whole cooldown. Returns the message, or None if the
        send failed.
        """
        try:
            self.notify(message)
        except Exception:
            log.exception("%s: alert NOT delivered, will retry next cycle: %s",
                          ticker, message)
            return None
        self._last_alerted[ticker] = time.monotonic()
        log.info("alert: %s", message)
        return message

    def _report_problem(self, ticker, exc):
        cooldown = self.config.alert_cooldown_seconds
        last = self._last_problem.get(ticker)
        if last is not None and time.monotonic() - last < cooldown:
            log.debug("%s: still broken, suppressed: %s", ticker, exc)
            return
        self._last_problem[ticker] = time.monotonic()
        log.error("%s", exc)
        try:
            self.notify(f"pipeline problem: {exc}")
        except Exception:
            log.exception("%s: could not send problem notice", ticker)

    # ── backfill ────────────────────────────────────────────────────

    def backfill_trades(self, hours=24, limit=200):
        """Seed the trade series from Kalshi history. Idempotent; safe to re-run.

        Stores only trades that actually executed. Gaps are left as gaps on
        purpose: padding a sparse series with the last known price shrinks the
        baseline's standard deviation, which inflates every z-score and causes
        exactly the false alerts the check exists to avoid.
        """
        total = {}
        for market in self.config.markets:
            ticker = market.ticker
            try:
                trades = self.kalshi.get_trades(ticker, limit=limit)
            except Exception as e:
                log.warning("%s: trade backfill failed: %s", ticker, e)
                total[ticker] = None
                continue
            cutoff = time.time() - hours * 3600
            window = [t for t in trades if t["ts"].timestamp() >= cutoff]
            # Kalshi returns the newest `limit` trades, so a wide window on a
            # busy market can be silently short: the oldest trade fetched may be
            # newer than the cutoff, meaning older trades exist that we never saw.
            if len(trades) >= limit:
                oldest = min(t["ts"].timestamp() for t in trades)
                if oldest > cutoff:
                    log.warning(
                        "%s: backfill window is %dh but the %d most recent trades only "
                        "reach back %.1fh; older history needs a larger limit or a "
                        "paged fetch", ticker, hours, limit, (time.time() - oldest) / 3600)
            added = self.storage.insert_trades(ticker, window)

            total[ticker] = added
            log.info("%s: %d trade(s) in the last %dh, %d new, %d stored total",
                     ticker, len(window), hours, added, self.storage.count_trades(ticker))
        return total

    # ── standalone loop ──────────────────────────────────────────────

    def stop(self):
        """Ask run_forever to finish the current cycle and return."""
        self._running = False

    def run_forever(self):
        def _handle(signum, _frame):
            log.info("signal %s received", signum)
            self.stop()

        # Only valid on the main thread, and only meaningful on POSIX -- which is
        # where this runs. On Windows SIGTERM is not deliverable this way, so
        # stop() is the portable escape hatch (Ctrl-C does work).
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        log.info(
            "polling %d markets every %ds (cooldown %ds)",
            len(self.config.markets),
            self.config.poll_interval_seconds,
            self.config.alert_cooldown_seconds,
        )
        while self._running:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                # poll_once deliberately lets unexpected errors through so a bug
                # is visible. A long-running daemon should still outlive one, so
                # this is the last resort -- CRITICAL with a traceback, not a
                # quiet retry. Under Airflow there is no wrapper like this, so
                # the same failure fails the task instead.
                log.critical("cycle failed, continuing", exc_info=True)
            if not self._running:
                break
            self._sleep_until(started + self.config.poll_interval_seconds)
        log.info("stopped")

    def _sleep_until(self, deadline):
        """Wait until `deadline` in slices, so a stop request is noticed promptly.

        A single time.sleep() would be uninterruptible: at a 2 minute interval a
        SIGTERM would not be honoured for up to 2 minutes, which is longer than
        Docker's 10s SIGKILL grace period, so shutdown would lose the race.
        """
        while self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.5))
