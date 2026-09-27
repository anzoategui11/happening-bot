"""
Central configuration for happening-bot.
Edit this file directly to change markets, thresholds, or polling behavior.
"""

from dataclasses import dataclass, field
from enum import Enum


class AlertType(str, Enum):
    PCT = "pct"
    STDEV = "stdev"


@dataclass(frozen=True)
class MarketAlert:
    type: AlertType = AlertType.PCT
    threshold: float = 5.0    # net % (e.g. 5.0) or std-dev multiplier (e.g. 2.5)
    lookback: int = 10        # used when type == PCT
    sample_size: int = 20     # used when type == STDEV

    def __post_init__(self):
        if self.threshold <= 0:
            raise ValueError("threshold must be positive")
        if self.type == AlertType.PCT and self.lookback < 1:
            raise ValueError("pct alerts require lookback >= 1")
        if self.type == AlertType.STDEV and self.sample_size < 3:
            # check_stdev excludes the newest tick from the baseline, leaving
            # prices[1:] to take a stdev over -- that needs 2 of its own points
            raise ValueError("stdev alerts require sample_size >= 3")


@dataclass(frozen=True)
class MarketConfig:
    ticker: str
    alert: MarketAlert = field(default_factory=MarketAlert)


@dataclass(frozen=True)
class PipelineConfig:
    poll_interval_seconds: int
    max_stored_samples: int
    markets: list[MarketConfig] = field(default_factory=list)

    def __post_init__(self):
        tickers = [m.ticker for m in self.markets]
        if len(tickers) != len(set(tickers)):
            raise ValueError("duplicate ticker in markets list")
        if not (1 <= len(self.markets) <= 20):
            raise ValueError("markets list must contain 1-20 tickers")
        for m in self.markets:
            needed = m.alert.lookback + 1 if m.alert.type == AlertType.PCT else m.alert.sample_size
            if needed > self.max_stored_samples:
                raise ValueError(
                    f"{m.ticker}: max_stored_samples ({self.max_stored_samples}) is too small "
                    f"for its alert, which needs at least {needed} stored samples — "
                    f"raise max_stored_samples or lower this market's lookback/sample_size"
                )


def markets_from_tickers(tickers: list[str], alert: MarketAlert = None) -> list[MarketConfig]:
    """Build a MarketConfig list from a plain list of ticker strings,
    all sharing the same alert settings (defaults if none given)."""
    alert = alert or MarketAlert()
    return [MarketConfig(ticker=t, alert=alert) for t in tickers]


# ── Edit below this line ─────────────────────────────────────────────

# Paste your resolved tickers here:
TICKERS = [
    "KXMACHADOVENEZUELA-26JUN29-OCT01",
    "KXVENEZOILCOS-27JAN01-SHEL",
    "KXVENEZOILCOS-27JAN01-EXXO",
    "KXVENEZOILCOS-27JAN01-CONO",
    "KXLEAVEOPEC-28JAN01-27JAN01",
]

CONFIG = PipelineConfig(
    poll_interval_seconds=60,
    max_stored_samples=50,   # keep >= the largest lookback/sample_size used below
    markets=markets_from_tickers(TICKERS),
    # To give one or more tickers different thresholds instead of the
    # shared default, build them separately and combine, e.g.:
    #   markets=markets_from_tickers(TICKERS[:-1])
    #           + markets_from_tickers(TICKERS[-1:], MarketAlert(type=AlertType.STDEV, threshold=2.5)),
)