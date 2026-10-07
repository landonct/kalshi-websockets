import base64
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

URL = "https://external-api.kalshi.com/trade-api/v2/markets"
SERIES_URL = "https://external-api.kalshi.com/trade-api/v2/series"


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


def get_live_markets(tickers: str, *, limit: int = 100, page_limit: int = 1000, max_retries: int = 5) -> list[str]:
    params: dict = {"status": "open", "limit": page_limit, "series_ticker": tickers}
    tickers: list[str] = []
    while True:
        backoff = 1
        retries = 0
        r = requests.get(URL, params=params, timeout=10)
        
        while r.status_code == 429 and retries < max_retries:
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = backoff
            else:
                delay = backoff
            
            print(f"Rate limited. Retrying ({retries + 1}/{max_retries}) after {delay} seconds...")
            time.sleep(delay)
            retries += 1
            backoff *= 2
            r = requests.get(URL, params=params, timeout=10)

        r.raise_for_status()


        data = r.json()
        new_tickers = [m["ticker"] for m in data.get("markets", [])]
        tickers.extend(new_tickers)

        if len(tickers) > limit:
            return tickers[:limit]

        cursor = data.get("cursor")
        if not cursor:
            return tickers
        params["cursor"] = cursor

def get_series_list(category: str, include_volume: bool = False, min_volume: float = 0, max_retries: int = 5) -> pd.DataFrame:
    backoff = 1
    retries = 0
    params = {"category": category, "include_volume": include_volume}
    tickers: list[str] = []
    volume: list[str] = []

    r = requests.get(SERIES_URL, params=params)

    while r.status_code == 429 and retries < max_retries:
        retry_after = r.headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = backoff
        else:
            delay = backoff
        
        print(f"Rate limited. Retrying ({retries + 1}/{max_retries}) after {delay} seconds...")
        time.sleep(delay)
        retries += 1
        backoff *= 2
        r = requests.get(URL, params=params, timeout=10)

    r.raise_for_status()

    data = r.json().get("series", [])
    new_tickers = [m.get("ticker", "") for m in data]
    new_volume = [float(m.get("volume_fp", np.nan)) for m in data]
    tickers.extend(new_tickers)
    volume.extend(new_volume)
    df = pd.DataFrame({"tickers": tickers, "volume": volume})
    df = df.sort_values("volume") if include_volume else df.sort_values("tickers")

    return df[df["volume"] > min_volume].reset_index(drop=True) if include_volume else df["tickers"].reset_index(drop=True)
