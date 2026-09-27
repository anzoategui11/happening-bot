import time, base64, re, requests
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_PATH = "/trade-api/v2"

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
        r = requests.get(self.base_url + path, headers=self._headers("GET", path), timeout=15)
        r.raise_for_status()
        return r.json()

    def get_market(self, ticker):
        return self.get(f"/markets/{ticker}")["market"]

