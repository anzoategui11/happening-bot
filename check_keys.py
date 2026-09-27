"""
Verify that KALSHI_KEY_ID and the private key PEM are actually a matching pair,
and that Kalshi accepts the signature.

Kalshi's API gives you no way to query which public key is registered against a
Key ID, and its failure mode is unhelpful: a mismatched pair returns the same
401 INCORRECT_API_KEY_SIGNATURE whether the PEM is corrupt, the Key ID is
typo'd, or the two simply came from different generations. This script tells
those cases apart.

Run from the project root:
    uv run python check_keys.py                    # uses KALSHI_PRIVATE_KEY_PATH
    uv run python check_keys.py other.pem          # check a candidate file
"""

import base64
import hashlib
import os
import sys

sys.path.append("src")  # must come BEFORE importing kalshi_client

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

from kalshi_client import KalshiClient

PROD_HOSTS = ["https://api.elections.kalshi.com", "https://external-api.kalshi.com"]
API_PATH = "/trade-api/v2"
BALANCE_PATH = "/portfolio/balance"


def load_key(pem_path):
    from kalshi_client import _load_pem
    return _load_pem(pem_path)


def fingerprint(public_key):
    """Stable identifier for a public key, so two files can be compared."""
    pem = public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(pem).hexdigest()[:32]


def check_roundtrip(key):
    """A corrupt or mis-encoded PEM fails here, before any network call."""
    probe = b"kalshi-key-check"
    sig = key.sign(probe, _pss(), hashes.SHA256())
    key.public_key().verify(sig, probe, _pss(), hashes.SHA256())


def _pss():
    return padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)


def main():
    pem_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    key_id = os.environ.get("KALSHI_KEY_ID", "")

    if not pem_path:
        sys.exit("No PEM path: pass one as an argument or set KALSHI_PRIVATE_KEY_PATH.")
    if not os.path.exists(pem_path):
        sys.exit(f"PEM not found at {pem_path!r}")

    print(f"PEM   {pem_path}")
    print(f"KeyID {key_id or '(unset)'}")

    try:
        key = load_key(pem_path)
    except Exception as e:
        sys.exit(f"FAIL  not a readable private key: {type(e).__name__}: {e}")

    print(f"Key   {type(key).__name__}, {key.key_size} bits")
    print(f"FPR   {fingerprint(key.public_key())}")

    try:
        check_roundtrip(key)
        print("Local OK - PEM signs and verifies against its own public key")
    except Exception as e:
        sys.exit(f"FAIL  PEM is corrupt or mis-encoded: {type(e).__name__}: {e}")

    # A private endpoint is the only thing that actually validates the signature;
    # public routes return 200 regardless of the key.
    print("\nLive auth against /portfolio/balance:")
    for host in PROD_HOSTS:
        client = KalshiClient(key_id, pem_path, base_url=host + API_PATH)
        r = requests.get(
            host + API_PATH + BALANCE_PATH,
            headers=client._headers("GET", BALANCE_PATH), timeout=12,
        )
        if r.status_code == 200:
            print(f"  {host:40} 200  OK  balance={r.json().get('balance')} cents")
            return
        # error bodies are sometimes JSON, sometimes empty text/plain
        try:
            detail = r.json().get("error", {}).get("details", "")
        except ValueError:
            detail = r.text[:60] or "(empty body)"
        print(f"  {host:40} {r.status_code}  {detail}")

    print(
        "\nFAIL  Kalshi rejected the signature. If the PEM is valid (it passed the\n"
        "      local check above), then this PEM and the Key ID are not a pair.\n"
        "      Mint a new API key and use the Key ID and PEM from that one request."
    )


if __name__ == "__main__":
    main()
