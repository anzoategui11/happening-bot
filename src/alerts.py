"""Pure threshold logic: decide whether a market has moved enough to alert.

Both checks take a `storage` object rather than importing a database handle,
which is the injection seam the tests use. check_pct reads the polled tick
series; check_stdev reads the executed-trade series. Each returns a formatted
alert message when its threshold is crossed, or None when it is not.
"""

import statistics

def check_pct(storage, ticker, threshold, lookback):
    """Percentage move against the polled series, not the trade series.

    A poll of the last traded price repeats a stale value between trades, which
    makes it useless for measuring variance -- but it is the right basis for
    this check, because it answers "how far has the price moved in the last
    `lookback` samples" and the *change* is real information even when most
    samples in between are identical.
    """
    # Fetch one more sample than the window so there is something to compare against.
    prices = storage.get_recent(ticker, lookback + 1)

    # Still warming up: not enough history to compare. Normal, not an error.
    if len(prices) <= lookback:
        return None
    # get_recent is newest-first, so prices[0] is now and prices[-1] is then.
    current, past = prices[0], prices[-1]
    # a zero base makes percentage change undefined; Kalshi reports 0.0000 for
    # markets that have never traded, so this is reachable rather than theoretical
    if past == 0:
        return None
    pct_change = (current - past) / past * 100
    if abs(pct_change) >= threshold:
        return f"{ticker}: {pct_change:+.1f}% over last {lookback} samples (now {current:.4f})"
    return None

def check_stdev(storage, ticker, threshold, sample_size, warn=None):
    """Volatility check against the trade series, not the polled series.

    A poll every 2 minutes of a market that trades once an hour repeats the
    same last price ~30 times, so the standard deviation of those samples
    measures polling frequency rather than the market -- on such a series it is
    0.0 and the check can never fire. Trades are sparse but every point is a
    real observation, which is what a baseline needs.

    `warn` is called with a human-readable reason whenever the check cannot
    report, so an illiquid or still-warming market is visible rather than
    silently returning None forever.
    """
    # misconfiguration, not warm-up: no amount of polling can satisfy this
    if sample_size < 3:
        raise ValueError(
            f"stdev needs sample_size >= 3, got {sample_size}: the newest trade is "
            "excluded from the baseline, so the baseline needs 2 trades of its own"
        )
    prices = storage.get_recent_trades(ticker, sample_size)
    # Warm-up, not misconfiguration: there is simply not enough history yet.
    if len(prices) < sample_size:
        if warn:
            warn(f"{ticker}: only {len(prices)} trade(s) stored, need {sample_size} "
                 f"for a {sample_size}-trade baseline -- still warming up, and if the "
                 "market rarely trades this may never be reached")
        return None
    # Baseline is every stored trade except the newest, so the outlier being
    # tested cannot inflate the standard deviation it is measured against.
    mean, sd = statistics.mean(prices[1:]), statistics.stdev(prices[1:])
    # A flat baseline has no spread to measure against, so a z-score is
    # meaningless here (and would divide by zero).
    if sd == 0:
        if warn:
            warn(f"{ticker}: all {sample_size} stored trades have the same price "
                 f"({prices[0]}), so there is no variation to measure -- this market "
                 "is too illiquid for a stdev alert")
        return None
    z = (prices[0] - mean) / sd
    # Two-sided: a sharp move in either direction is equally worth reporting.
    if abs(z) >= threshold:
        return f"{ticker}: {z:+.2f}σ move (now {prices[0]:.4f}, mean {mean:.4f})"
    return None

