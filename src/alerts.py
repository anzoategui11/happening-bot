import statistics

def check_pct(storage, ticker, threshold, lookback):
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

def check_stdev(storage, ticker, threshold, sample_size):
    prices = storage.get_recent(ticker, sample_size)
    # the newest tick is excluded from the baseline, so the baseline needs at
    # least two points of its own for a standard deviation to exist
    if len(prices) < max(sample_size, 3):
        return None
    mean, sd = statistics.mean(prices[1:]), statistics.stdev(prices[1:])
    if sd == 0:
        return None
    z = (prices[0] - mean) / sd
    if abs(z) >= threshold:
        return f"{ticker}: {z:+.2f}σ move (now {prices[0]:.4f}, mean {mean:.4f})"
    return None
