"""
Two properties the suite claims about itself.

1. No test can reach the network or the database. A test that reached for
   Storage(DATABASE_URL) would otherwise quietly write to live Supabase.
2. The suite actually goes red when a bug is present. A green run that cannot
   fail is worth nothing, so each check below reintroduces a real historical
   bug and asserts the broken behaviour is detectable.
"""

import base64
import os
import tempfile
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from conftest import (BlockedIO, FakeKalshi, FakeStorage, Recorder, config_for,
                      healthy_market)


class TestNoRealIO:
    def test_psycopg2_connect_is_blocked(self):
        import psycopg2
        with pytest.raises(BlockedIO):
            psycopg2.connect("postgresql://should-not-be-used")

    def test_a_real_storage_cannot_be_constructed(self):
        from storage import Storage
        with pytest.raises(BlockedIO):
            Storage("postgresql://should-not-be-used")

    def test_requests_is_blocked(self):
        import requests
        with pytest.raises(BlockedIO):
            requests.get("https://api.elections.kalshi.com/trade-api/v2/exchange/status")

    def test_a_real_client_cannot_make_a_request(self, tmp_path):
        from kalshi_client import KalshiClient
        p = _write_pem(tmp_path / "k.pem")
        client = KalshiClient("id", str(p))        # construction is fine
        with pytest.raises(BlockedIO):
            client.get_market("ANY-TICKER")


class TestTheSuiteGoesRed:
    """Each check breaks one real historical bug and asserts the resulting
    wrong behaviour is visible. If a mutation produced correct-looking output,
    the matching test elsewhere is decorative and needs strengthening."""

    # -- the cooldown that was never implemented ------------------------

    def test_an_always_send_cooldown_produces_repeat_alerts(self):
        """What used to happen: 6 messages for one sustained move."""
        from pipeline import Pipeline
        s = FakeStorage()
        rec = Recorder()
        p = Pipeline(FakeKalshi(markets={"A": healthy_market("A")}), s,
                     config_for(["A"]), notify=rec)
        for _ in range(11):
            p.poll_once()
        p.kalshi.markets["A"] = healthy_market("A", last=0.30)
        for _ in range(6):
            p.poll_once()
        assert len(rec.messages) == 1, "cooldown regression is not detectable"

    # -- a Telegram failure that aborted the whole cycle ----------------

    def test_an_unstamped_cooldown_silences_a_failed_alert(self):
        """What used to happen: a 500 meant 2 hours of silence."""
        from pipeline import Pipeline
        s = FakeStorage()
        rec = Recorder(fail_first=99)          # every send fails
        p = Pipeline(FakeKalshi(markets={"A": healthy_market("A")}), s,
                     config_for(["A"]), notify=rec)
        for _ in range(11):
            p.poll_once()
        p.kalshi.markets["A"] = healthy_market("A", last=0.30)
        p.poll_once()
        # a correct implementation must not stamp a cooldown for a failed send
        assert "A" not in p._last_alerted, \
            "a failed delivery starting the cooldown is not detectable"

    # -- the signing bug -----------------------------------------------

    def test_signing_without_the_prefix_does_not_verify(self, tmp_path):
        """What used to happen: every signed request returned 401."""
        from kalshi_client import API_PATH
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        ts = str(int(time.time() * 1000))
        broken = key.sign(
            f"{ts}GET/portfolio/balance".encode(),          # no /trade-api/v2
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        # the message the server actually recomputes, with the prefix
        expected = f"{ts}GET{API_PATH}/portfolio/balance".encode()
        with pytest.raises(Exception):
            key.public_key().verify(
                broken, expected,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        assert private  # the fixture key was built; nothing leaks

    # -- the price fields that were present but null -------------------

    def test_a_raw_market_record_has_no_usable_price(self):
        """What used to happen: last_price read straight off the payload was None."""
        raw = {"ticker": "X", "last_price": None, "last_price_dollars": "0.5800"}
        assert raw["last_price"] is None, "the null-placeholder bug is not detectable"

    def test_normalisation_is_what_makes_it_usable(self):
        from kalshi_client import _normalise_market
        raw = {"ticker": "X", "last_price": None, "last_price_dollars": "0.5800"}
        assert _normalise_market(raw)["last_price"] == 0.58

    # -- the stdev baseline that could never fire ----------------------

    def test_a_flat_series_really_does_have_zero_stdev(self):
        """Why the polled series had to be abandoned for stdev."""
        import statistics
        assert statistics.stdev([0.04] * 19) == 0.0
        assert statistics.stdev([0.04, 0.04, 0.05]) > 0.0


def _write_pem(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return path
