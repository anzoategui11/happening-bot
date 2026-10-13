"""Signed REST client for Kalshi, plus price-field normalisation.

Kalshi authenticates every request with an RSA-PSS signature over
"<timestamp><method><path>". The client also papers over a Kalshi quirk: the
legacy integer-cent price keys are still present in responses but are
permanently null, so `_normalise_market` back-fills them from the current
dollar-denominated fields.
"""

import time, base64, re, requests
from datetime import datetime
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_PATH = "/trade-api/v2"

# Kalshi moved to dollar-denominated price fields and left the old integer-cent
# keys in the payload as permanently-null placeholders, so reading last_price or
# yes_bid silently yields None. Map the old names onto the live ones.
_PRICE_FIELDS = {
    "last_price": "last_price_dollars",
    "previous_price": "previous_price_dollars",
    "yes_bid": "yes_bid_dollars",
    "yes_ask": "yes_ask_dollars",
    "no_bid": "no_bid_dollars",
    "no_ask": "no_ask_dollars",
    "volume": "volume_fp",
    "volume_24h": "volume_24h_fp",
    "open_interest": "open_interest_fp",
    "liquidity": "liquidity_dollars",
    "yes_bid_size": "yes_bid_size_fp",
    "yes_ask_size": "yes_ask_size_fp",
    "no_bid_size": "no_bid_size_fp",
    "no_ask_size": "no_ask_size_fp",
}

def _num(value):
    """Dollar and size fields arrive as decimal strings; prices are 0.00-1.00."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _normalise_market(market):
    """Fill the legacy price keys from the current dollar-denominated ones."""
    out = dict(market)
    for legacy, current in _PRICE_FIELDS.items():
        if out.get(legacy) in (None, ""):
            out[legacy] = _num(out.get(current))
    return out

def _load_pem(path):
    """Load a private key, tolerating the single-line PEM that Kalshi serves.

    The dashboard downloads the whole key as one line with no newlines, which
    OpenSSL refuses to parse, so re-wrap the base64 body at 64 columns first.
    """
    with open(path, "rb") as f:
        raw = f.read()
    m = re.search(rb"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", raw, re.S)
    if m:
        body = re.sub(rb"\s+", b"", m.group(2))
        wrapped = b"\n".join(body[i:i + 64] for i in range(0, len(body), 64))
        raw = (b"-----BEGIN " + m.group(1) + b"-----\n" + wrapped
               + b"\n-----END " + m.group(1) + b"-----\n")
    return serialization.load_pem_private_key(raw, password=None)

class KalshiClient:
    """Minimal signed GET client for the Kalshi trade API."""

    def __init__(self, key_id, private_key_path, base_url="https://api.elections.kalshi.com" + API_PATH):
        self.key_id = key_id
        self.base_url = base_url
        # Kalshi signs the exact request path, /trade-api/v2 prefix included.
        # Derive it from base_url so the signature can never drift from the URL sent.
        self._prefix = urlparse(base_url).path.rstrip("/")
        self.private_key = _load_pem(private_key_path)

    def _headers(self, method, path):
        ts = str(int(time.time() * 1000))
        # query string is excluded from the signed message
        msg = f"{ts}{method}{self._prefix}{path.split('?')[0]}".encode()
        sig = self.private_key.sign(
            msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def get(self, path):
        """Signed GET. Returns the parsed JSON body."""
        # timeout is bounded so a hung request cannot stall the whole poll cycle.
        r = requests.get(self.base_url + path, headers=self._headers("GET", path), timeout=15)
        r.raise_for_status()
        return r.json()

    def get_market(self, ticker):
        return _normalise_market(self.get(f"/markets/{ticker}")["market"])

    def get_trades(self, ticker, limit=200):
        """Executed trades, newest first, as {trade_id, ts, price}.

        These are real observations at their own timestamps, unlike a poll of
        the last price, which repeats a stale value between trades. Trades with
        no yes-side price are dropped rather than stored as null.
        """
        out = []
        for t in self.get(f"/markets/trades?ticker={ticker}&limit={limit}").get("trades", []):
            price = _num(t.get("yes_price_dollars"))
            created = t.get("created_time")
            if price is None or not t.get("trade_id") or not created:
                continue
            out.append({
                "trade_id": t["trade_id"],
                "ts": datetime.fromisoformat(created.replace("Z", "+00:00")),
                "price": price,
            })
        return out


