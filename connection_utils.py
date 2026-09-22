import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

URL = "https://external-api.kalshi.com/trade-api/v2/markets"


def load_private_key_from_file(file_path):
    with open(file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,  # or provide a password if your key is encrypted
            backend=default_backend(),
        )
    return private_key

def sign_pss_text(private_key: rsa.RSAPrivateKey, text: str) -> str:
    message = text.encode("utf-8")
    try:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")
    except InvalidSignature as e:
        raise ValueError("RSA sign PSS failed") from e


def create_headers(private_key, method: str, path: str, key: str) -> dict:
    """Create authentication headers"""
    timestamp = str(int(datetime.now(ZoneInfo("America/New_York")).timestamp() * 1000))
    msg_string = timestamp + method + path.split("?")[0]
    signature = sign_pss_text(private_key, msg_string)

    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": key,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }

def get_live_markets(limit: int = 1000) -> list[str]:
    params = {"status": "open", "limit": limit}
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        response = requests.get(URL, params=params)

        response.raise_for_status()
        data = response.json()
        market_data = data.get("markets")

        tickers = market_data.get("ticker")

