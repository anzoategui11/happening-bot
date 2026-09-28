import statistics

def check_pct(storage, ticker, threshold, lookback):
    """Percentage move against the polled series, not the trade series.

    A poll of the last traded price repeats a stale value between trades, which
    makes it useless for measuring variance -- but it is the right basis for
    this check, because it answers "how far has the price moved in the last
    `lookback` samples" and the *change* is real information even when most
    samples in between are identical.
    """
    prices = storage.get_recent(ticker, lookback + 1)

    if len(prices) <= lookback:
        return None
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
    if len(prices) < sample_size:
        if warn:
            warn(f"{ticker}: only {len(prices)} trade(s) stored, need {sample_size} "
                 f"for a {sample_size}-trade baseline -- still warming up, and if the "
                 "market rarely trades this may never be reached")
        return None
    mean, sd = statistics.mean(prices[1:]), statistics.stdev(prices[1:])
    if sd == 0:
        if warn:
            warn(f"{ticker}: all {sample_size} stored trades have the same price "
                 f"({prices[0]}), so there is no variation to measure -- this market "
                 "is too illiquid for a stdev alert")
        return None
    z = (prices[0] - mean) / sd
    if abs(z) >= threshold:
        return f"{ticker}: {z:+.2f}σ move (now {prices[0]:.4f}, mean {mean:.4f})"
    return None

