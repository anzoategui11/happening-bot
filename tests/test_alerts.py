"""
alerts.py holds the only real decision logic in the project, and every bug
found while building it was a silent wrong answer rather than a crash. These
tests pin the behaviours that are easy to regress:

- warm-up returns None, misconfiguration raises, and the two stay distinct
- a zero base cannot divide by zero
- check_pct reads the polled series and check_stdev reads the trade series
"""

import pytest

from alerts import check_pct, check_stdev

from conftest import FakeStorage


# ── check_pct ────────────────────────────────────────────────────────

class TestCheckPct:
    """Percentage move against the polled tick series."""

    def test_fires_on_a_large_move(self):
        # 0.30 against 0.50 two samples back is -40.0%
        s = FakeStorage(ticks={"A": [0.30, 0.50, 0.50]})
        msg = check_pct(s, "A", threshold=5.0, lookback=2)
        assert msg is not None
        assert "-40.0%" in msg

    def test_stays_quiet_on_a_small_move(self):
        s = FakeStorage(ticks={"A": [0.51, 0.50, 0.50]})
        assert check_pct(s, "A", threshold=5.0, lookback=2) is None

    def test_warm_up_returns_none(self):
        """Not enough samples is normal, not an error."""
        s = FakeStorage(ticks={"A": [0.50, 0.50]})
        assert check_pct(s, "A", threshold=5.0, lookback=10) is None

    def test_zero_base_does_not_divide_by_zero(self):
        """Kalshi reports 0.0000 for a market that has never traded."""
        s = FakeStorage(ticks={"A": [0.05, 0.0, 0.0]})
        assert check_pct(s, "A", threshold=5.0, lookback=2) is None

    def test_both_sides_of_zero_are_none(self):
        s = FakeStorage(ticks={"A": [0.0, 0.0, 0.0]})
        assert check_pct(s, "A", threshold=5.0, lookback=2) is None

    def test_reads_the_polled_series_not_trades(self):
        """The split is load-bearing: polling is right for pct, wrong for stdev."""
        s = FakeStorage(ticks={"A": [0.30, 0.55, 0.55]},
                        trades={"A": [0.30, 0.55, 0.55]})
        check_pct(s, "A", threshold=5.0, lookback=2)
        assert s.calls == ["get_recent"]
        assert "get_recent_trades" not in s.calls

    def test_compares_newest_against_oldest(self):
        """Depends on get_recent being newest-first; see AGENTS.md.

        Reversing the series inverts the sign of every alert without raising,
        which is exactly why this is asserted rather than assumed.
        """
        newest_first = FakeStorage(ticks={"A": [0.30, 0.50, 0.50]})
        oldest_first = FakeStorage(ticks={"A": [0.50, 0.50, 0.30]})
        fwd = check_pct(newest_first, "A", threshold=5.0, lookback=2)
        rev = check_pct(oldest_first, "A", threshold=5.0, lookback=2)
        assert fwd is not None and "-40.0%" in fwd
        # 0.50 against 0.30 the other way round: a completely different number
        assert rev is not None and "+66.7%" in rev
        assert fwd.split(": ")[1][0] != rev.split(": ")[1][0]

    def test_works_with_decimal_prices(self):
        """Postgres numeric comes back as Decimal, not float."""
        from decimal import Decimal
        s = FakeStorage(ticks={"A": [Decimal("0.30"), Decimal("0.50"), Decimal("0.50")]})
        msg = check_pct(s, "A", threshold=5.0, lookback=2)
        assert msg is not None and "-40.0%" in msg

    def test_formats_price_to_four_places(self):
        s = FakeStorage(ticks={"A": [0.3, 0.55, 0.55]})
        assert "now 0.3000" in check_pct(s, "A", threshold=5.0, lookback=2)


# ── check_stdev ──────────────────────────────────────────────────────

class TestCheckStdevRaisesOnMisconfiguration:
    """A sample_size below 3 is a config error, not a warm-up state."""

    @pytest.mark.parametrize("sample_size", [0, 1, 2])
    def test_sample_size_below_three_raises(self, sample_size):
        s = FakeStorage(trades={"A": [0.5, 0.6, 0.7]})
        with pytest.raises(ValueError, match="sample_size >= 3"):
            check_stdev(s, "A", threshold=2.5, sample_size=sample_size)

    def test_raises_before_touching_storage(self):
        """A config error should not need data to detect."""
        s = FakeStorage()
        with pytest.raises(ValueError):
            check_stdev(s, "A", threshold=2.5, sample_size=2)
        assert s.calls == []

    def test_distinct_from_warm_up(self):
        """Raising here is the point: it must not be a silent None."""
        s = FakeStorage()
        with pytest.raises(ValueError):
            check_stdev(s, "A", threshold=2.5, sample_size=2, warn=lambda m: None)


class TestCheckStdevAgainstTrades:
    """Z-score of the newest trade against the stored trade baseline."""

    def test_reads_the_trade_series_not_polls(self):
        s = FakeStorage(trades={"A": [0.9, 0.5, 0.5, 0.5]},
                        ticks={"A": [0.9, 0.5, 0.5, 0.5]})
        check_stdev(s, "A", threshold=2.5, sample_size=4)
        assert s.calls == ["get_recent_trades"]
        assert "get_recent" not in s.calls

    def test_fires_on_an_outlier(self):
        s = FakeStorage(trades={"A": [0.38, 0.04, 0.04, 0.04, 0.04, 0.05]})
        msg = check_stdev(s, "A", threshold=2.5, sample_size=6)
        assert msg is not None and "σ" in msg

    def test_stays_quiet_for_a_normal_move(self):
        s = FakeStorage(trades={"A": [0.51, 0.50, 0.52, 0.49, 0.51]})
        assert check_stdev(s, "A", threshold=2.5, sample_size=5) is None

    def test_warm_up_warns_and_returns_none(self):
        s = FakeStorage(trades={"A": [0.16, 0.17]})
        warns = []
        assert check_stdev(s, "A", threshold=2.5, sample_size=20,
                           warn=warns.append) is None
        assert len(warns) == 1
        assert "only 2 trade(s) stored" in warns[0]
        assert "may never be reached" in warns[0]

    def test_no_variation_warns_and_returns_none(self):
        """The bug this whole design exists to fix: a flat series has sd 0.0."""
        s = FakeStorage(trades={"A": [0.04] * 6})
        warns = []
        assert check_stdev(s, "A", threshold=2.5, sample_size=6,
                           warn=warns.append) is None
        assert len(warns) == 1
        assert "no variation" in warns[0]
        assert "too illiquid" in warns[0]

    def test_warn_is_optional(self):
        s = FakeStorage(trades={"A": [0.04] * 6})
        assert check_stdev(s, "A", threshold=2.5, sample_size=6) is None

    def test_works_with_decimal_prices(self):
        from decimal import Decimal
        s = FakeStorage(trades={"A": [Decimal("0.38")] + [Decimal("0.04")] * 4
                                    + [Decimal("0.05")]})
        assert check_stdev(s, "A", threshold=2.5, sample_size=6) is not None

    def test_scale_invariant(self):
        """Cents to dollars happened; a z-score must not notice."""
        cents = FakeStorage(trades={"A": [38, 4, 4, 4, 4, 5]})
        dollars = FakeStorage(trades={"A": [0.38, 0.04, 0.04, 0.04, 0.04, 0.05]})
        a = check_stdev(cents, "A", threshold=2.5, sample_size=6)
        b = check_stdev(dollars, "A", threshold=2.5, sample_size=6)
        assert a is not None and b is not None
        assert a.split("σ")[0].split(": ")[-1] == b.split("σ")[0].split(": ")[-1]

    def test_excludes_the_newest_trade_from_the_baseline(self):
        """Otherwise the outlier inflates the very stdev it is measured against."""
        s = FakeStorage(trades={"A": [0.90, 0.50, 0.51, 0.49]})
        msg = check_stdev(s, "A", threshold=2.0, sample_size=4)
        # baseline is [0.50, 0.51, 0.49]: mean 0.50, sd ~0.01, z ~+40
        assert "mean 0.5000" in msg
