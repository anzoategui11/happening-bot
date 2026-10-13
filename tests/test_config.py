"""
config.py validates itself at import, which means a bad setting is a startup
failure rather than a silent misconfiguration two hours later. These tests pin
each rule, because the two-series split introduced a second retention cap that
had to be kept consistent with the alerts.
"""

import pytest

from config import (AlertType, CONFIG, MarketAlert, MarketConfig, PipelineConfig,
                    markets_from_tickers)


def make(markets=("A",), alert=None, **overrides):
    """Build a valid PipelineConfig, with **overrides to poke a single field."""
    kwargs = dict(poll_interval_seconds=120, max_stored_samples=50,
                  markets=markets_from_tickers(list(markets), alert))
    kwargs.update(overrides)
    return PipelineConfig(**kwargs)


def pct(lookback=10, threshold=5.0):
    """Shorthand for a percentage-move alert."""
    return MarketAlert(type=AlertType.PCT, threshold=threshold, lookback=lookback)


class TestMarketAlert:
    """MarketAlert rejects impossible settings at construction."""

    def test_rejects_non_positive_threshold(self):
        with pytest.raises(ValueError, match="threshold must be positive"):
            MarketAlert(threshold=0)

    def test_pct_requires_a_lookback(self):
        with pytest.raises(ValueError, match="lookback >= 1"):
            MarketAlert(type=AlertType.PCT, lookback=0)

    @pytest.mark.parametrize("size", [0, 1, 2])
    def test_stdev_requires_three_samples(self, size):
        """check_stdev excludes the newest trade from the baseline, leaving it
        with size-1 points, and statistics.stdev needs two of its own."""
        with pytest.raises(ValueError, match="sample_size >= 3"):
            MarketAlert(type=AlertType.STDEV, sample_size=size)

    def test_stdev_accepts_three(self):
        assert MarketAlert(type=AlertType.STDEV, sample_size=3)

    def test_defaults_are_sane(self):
        a = MarketAlert()
        assert a.type is AlertType.PCT
        assert a.threshold == 5.0
        assert a.lookback == 10
        assert a.sample_size == 20


class TestPipelineConfig:
    """Every cross-field rule PipelineConfig enforces at import."""

    def test_rejects_duplicate_tickers(self):
        with pytest.raises(ValueError, match="duplicate"):
            make(markets=("A", "A"))

    def test_rejects_no_markets(self):
        with pytest.raises(ValueError, match="1-20"):
            make(markets=())

    def test_rejects_more_than_twenty(self):
        with pytest.raises(ValueError, match="1-20"):
            make(markets=tuple(f"T{i}" for i in range(21)))

    def test_accepts_exactly_twenty(self):
        assert len(make(markets=tuple(f"T{i}" for i in range(20))).markets) == 20

    def test_rejects_a_lookback_wider_than_retention(self):
        with pytest.raises(ValueError, match="max_stored_samples"):
            make(alert=pct(lookback=99), max_stored_samples=50)

    def test_lookback_plus_one_must_fit(self):
        """check_pct fetches lookback+1 rows, so lookback=50 needs 51."""
        with pytest.raises(ValueError, match="max_stored_samples"):
            make(alert=pct(lookback=50), max_stored_samples=50)
        assert make(alert=pct(lookback=49), max_stored_samples=50)

    def test_stdev_sample_size_must_fit_the_trade_cap(self):
        """check_stdev reads the trade series, which has its own retention cap."""
        alert = MarketAlert(type=AlertType.STDEV, sample_size=20)
        with pytest.raises(ValueError, match="max_stored_trades"):
            PipelineConfig(poll_interval_seconds=120, max_stored_samples=50,
                           max_stored_trades=10,
                           markets=[MarketConfig("A", alert)])

    def test_stdev_sample_size_fits_the_default_trade_cap(self):
        alert = MarketAlert(type=AlertType.STDEV, sample_size=20)
        assert PipelineConfig(poll_interval_seconds=120, max_stored_samples=50,
                              markets=[MarketConfig("A", alert)])

    @pytest.mark.parametrize("value", [0, 2])
    def test_trade_cap_must_leave_room_for_a_baseline(self, value):
        with pytest.raises(ValueError, match="max_stored_trades"):
            make(max_stored_trades=value)

    def test_rejects_a_zero_poll_interval(self):
        with pytest.raises(ValueError, match="poll_interval_seconds"):
            make(poll_interval_seconds=0)

    def test_rejects_a_negative_cooldown(self):
        with pytest.raises(ValueError, match="alert_cooldown_seconds"):
            make(alert_cooldown_seconds=-1)

    def test_zero_cooldown_is_allowed(self):
        assert make(alert_cooldown_seconds=0).alert_cooldown_seconds == 0


class TestLiveConfig:
    """The config the bot actually runs with, asserted so an edit that makes it
    unsatisfiable fails here rather than in production."""

    def test_imports_cleanly(self):
        assert isinstance(CONFIG, PipelineConfig)

    def test_markets_are_unique_and_within_limits(self):
        tickers = [m.ticker for m in CONFIG.markets]
        assert len(tickers) == len(set(tickers))
        assert 1 <= len(tickers) <= 20

    def test_every_market_can_fill_its_window(self):
        for m in CONFIG.markets:
            needed = (m.alert.lookback + 1 if m.alert.type is AlertType.PCT
                      else m.alert.sample_size)
            assert needed <= CONFIG.max_stored_samples, m.ticker
            if m.alert.type is AlertType.STDEV:
                assert needed <= CONFIG.max_stored_trades, m.ticker

    def test_tickers_look_like_kalshi_tickers(self):
        for m in CONFIG.markets:
            assert m.ticker.startswith("KX"), m.ticker
            assert " " not in m.ticker

    def test_poll_interval_and_cooldown_are_sane(self):
        assert CONFIG.poll_interval_seconds >= 30
        assert CONFIG.alert_cooldown_seconds >= CONFIG.poll_interval_seconds


class TestMarketsFromTickers:
    """The helper that expands a ticker list into MarketConfigs."""

    def test_shares_one_alert_across_tickers(self):
        alert = MarketAlert(threshold=9.0)
        markets = markets_from_tickers(["A", "B"], alert)
        assert all(m.alert is alert for m in markets)
        assert [m.ticker for m in markets] == ["A", "B"]

    def test_defaults_share_one_alert_instance(self):
        """Sharing is safe because both dataclasses are frozen, and it is what
        "all sharing the same settings" means in the docstring."""
        markets = markets_from_tickers(["A", "B"])
        assert markets[0].alert == markets[1].alert

    def test_an_explicit_alert_is_used_verbatim(self):
        alert = MarketAlert(threshold=9.0)
        markets = markets_from_tickers(["A", "B"], alert)
        assert all(m.alert is alert for m in markets)
