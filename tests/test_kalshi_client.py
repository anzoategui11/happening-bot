"""
kalshi_client.py has three failure modes that cost real time, none of which
raise: a signature computed over the wrong string, price fields that are
present but permanently null, and a PEM the dashboard serves unparseable.

All three are asserted here without a network call.
"""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_client import API_PATH, KalshiClient, _load_pem, _normalise_market, _num


# ── _num ─────────────────────────────────────────────────────────────

class TestNum:
    """_num converts Kalshi's stringy numbers, and nulls them out safely."""

    @pytest.mark.parametrize("raw,expected", [
        ("0.5800", 0.58),
        ("0", 0.0),
        (1, 1.0),
        (0.58, 0.58),
    ])
    def test_converts_numbers_and_digit_strings(self, raw, expected):
        assert _num(raw) == expected

    @pytest.mark.parametrize("raw", [None, ""])
    def test_tolerates_null_placeholders(self, raw):
        """Kalshi leaves the old field names present but null. A TypeError here
        would surface much later, in the alert maths."""
        assert _num(raw) is None

    @pytest.mark.parametrize("raw", ["n/a", "abc", [], {}])
    def test_unparseable_becomes_none_not_an_exception(self, raw):
        assert _num(raw) is None


# ── _normalise_market ────────────────────────────────────────────────

class TestNormaliseMarket:
    """Back-fill the legacy price keys from the live dollar fields."""

    @pytest.fixture
    def raw(self):
        return {
            "ticker": "X", "status": "active",
            "last_price": None, "yes_bid": None, "yes_ask": None, "volume": None,
            "last_price_dollars": "0.5800", "yes_bid_dollars": "0.5800",
            "yes_ask_dollars": "0.6600", "volume_fp": "2405.12",
            "volume_24h_fp": "0.36", "open_interest_fp": "1121.99",
            "liquidity_dollars": "0.0000",
            "yes_bid_size_fp": "14.16", "yes_ask_size_fp": "200.00",
        }

    def test_backfills_the_deprecated_names(self, raw):
        out = _normalise_market(raw)
        assert out["last_price"] == 0.58
        assert out["yes_bid"] == 0.58
        assert out["yes_ask"] == 0.66
        assert out["volume"] == 2405.12

    def test_converts_to_float_not_string(self, raw):
        assert isinstance(_normalise_market(raw)["last_price"], float)

    def test_preserves_the_new_field_names(self, raw):
        out = _normalise_market(raw)
        assert out["last_price_dollars"] == "0.5800"

    def test_does_not_overwrite_a_populated_legacy_value(self, raw):
        """A future Kalshi fix that repopulates the old names should win."""
        raw["last_price"] = 99
        assert _normalise_market(raw)["last_price"] == 99

    def test_preserves_absent_fields_as_none(self, raw):
        out = _normalise_market(raw)
        assert out["previous_price"] is None

    def test_does_not_mutate_the_input(self, raw):
        _normalise_market(raw)
        assert raw["last_price"] is None

    def test_survives_a_market_with_no_price_fields(self):
        out = _normalise_market({"ticker": "X"})
        assert out["last_price"] is None


# ── _load_pem ────────────────────────────────────────────────────────

@pytest.fixture
def pem_bytes():
    """A throwaway 2048-bit key, generated rather than committed.

    Keeping a real private key in a test fixture would be the one thing in this
    repo that could leak a credential into git history.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        # the member is PKCS8; only the string value contains a '#'
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class TestLoadPem:
    """_load_pem tolerates the single-line, ragged PEMs Kalshi hands out."""

    def test_reads_a_normal_multiline_pem(self, pem_bytes):
        assert _load_pem(_write(pem_bytes)) is not None

    def test_reads_the_single_line_pem_kalshi_serves(self, pem_bytes):
        """The dashboard downloads the key with no newlines at all, which
        OpenSSL refuses to parse without re-wrapping."""
        flattened = b"".join(pem_bytes.split(b"\n"))
        key = _load_pem(_write(flattened))
        assert key.key_size == 2048

    def test_tolerates_irregular_whitespace(self, pem_bytes):
        """Strip the markers and re-lay the body with ragged indents and
        line lengths, which is what a hand-edited or copy-pasted key looks like."""
        body = b"".join(pem_bytes.split(b"\n")[1:-2])
        mangled = b"\n  ".join(body[i:i + 37] for i in range(0, len(body), 37))
        wrapped = (b"-----BEGIN PRIVATE KEY-----\n" + mangled
                   + b"\n-----END PRIVATE KEY-----\n")
        assert _load_pem(_write(wrapped)) is not None

    def test_rejects_something_that_is_not_a_key(self):
        with pytest.raises(ValueError):
            _load_pem(_write(b"just some text, not a key at all"))

    def test_missing_file_raises(self):
        with pytest.raises(OSError):
            _load_pem("no-such-file.pem")


# ── signing ──────────────────────────────────────────────────────────

class TestSigning:
    """Regression: the signature covered the endpoint path but not the
    /trade-api/v2 prefix, so every signed request returned 401. Public
    endpoints still returned 200, which is what hid it."""

    @pytest.fixture
    def client(self, pem_bytes):
        return KalshiClient("key-id", _write(pem_bytes))

    def _verify(self, client, path, method="GET"):
        return client._prefix, path

    def test_prefix_comes_from_base_url(self, client):
        assert client._prefix == API_PATH

    def test_prefix_tracks_a_custom_base_url(self, pem_bytes):
        c = KalshiClient("k", _write(pem_bytes),
                         base_url="https://external-api.kalshi.com" + API_PATH)
        assert c._prefix == API_PATH

    def test_a_host_without_a_path_gives_an_empty_prefix(self, pem_bytes):
        c = KalshiClient("k", _write(pem_bytes), base_url="https://example.com")
        assert c._prefix == ""

    def test_signature_verifies_against_the_prefixed_path(self, client, monkeypatch):
        """Sign a known message the way the client does, then verify it.

        This pins the exact string that must be signed: prefix + path, with the
        query string removed.
        """
        fixed = 1758000000000
        monkeypatch.setattr("kalshi_client.time.time", lambda: fixed / 1000)
        path = "/portfolio/balance"
        headers = client._headers("GET", path)

        ts = str(fixed)
        expected = f"{ts}GET{API_PATH}{path}".encode()
        client.private_key.public_key().verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), expected,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())

    def test_query_string_is_excluded_from_the_signature(self, client, monkeypatch):
        fixed = 1758000000000
        monkeypatch.setattr("kalshi_client.time.time", lambda: fixed / 1000)
        plain = client._headers("GET", "/markets?status=open")
        with_qs = client._headers("GET", "/markets?status=open&limit=5")
        # same path, different query -> both must verify against the bare path
        for h in (plain, with_qs):
            client.private_key.public_key().verify(
                base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                f"{fixed}GET{API_PATH}/markets".encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                            salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256())

    def test_header_names_match_the_api(self, client):
        h = client._headers("GET", "/portfolio/balance")
        assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE",
                          "KALSHI-ACCESS-TIMESTAMP"}
        assert h["KALSHI-ACCESS-KEY"] == "key-id"

    def test_signature_is_base64(self, client):
        h = client._headers("GET", "/portfolio/balance")
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])


def _write(data):
    """Write bytes to a temp .pem file and return its path (caller cleans up)."""
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path
