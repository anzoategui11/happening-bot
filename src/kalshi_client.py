import time, base64, requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

class KalshiClient:
    def __init__(self, key_id, private_key_path, base_url="https://api.elections.kalshi.com/trade-api/v2"):
        self.key_id = key_id
        self.base_url = base_url
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _headers(self, method, path):
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path}".encode()
        sig = self.private_key.sign(
            msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def get_market(self, ticker):
        path = f"/markets/{ticker}"
        r = requests.get(self.base_url + path, headers=self._headers("GET", path))
        r.raise_for_status()
        return r.json()["market"]
