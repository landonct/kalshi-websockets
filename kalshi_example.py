#! /bin/python
import asyncio
import base64
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import websockets
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv


def load_private_key_from_file(file_path):
    with open(file_path, "rb") as key_file:
        private_key = serialization.load_pem_private_key(
            key_file.read(),
            password=None,  # or provide a password if your key is encrypted
            backend=default_backend(),
        )
    return private_key


load_dotenv(".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
MARKET_TICKER = "KXNFLGAME-26AUG22WASDET-WAS"  # KXFEDDECISION-26SEP-H25" KXMLBGAME-26AUG191235DETPIT-DET

method = "GET"


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


def create_headers(private_key, method: str, path: str) -> dict:
    """Create authentication headers"""
    timestamp = str(int(datetime.now(ZoneInfo("America/New_York")).timestamp() * 1000))
    msg_string = timestamp + method + path.split("?")[0]
    signature = sign_pss_text(private_key, msg_string)

    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_ACCESS_KEY,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


class OrderBook:
    """Maintains a local Kalshi order book from snapshot + delta messages.

    Kalshi expresses the book as two sets of *bids*:
      - "yes" side: resting orders to BUY Yes at price p            -> a Yes bid at p
      - "no"  side: resting orders to BUY No  at price q, which is
                    equivalent to an offer to SELL Yes at (1 - q)   -> a Yes ask

    So to view the market in normal bid/ask terms (denominated in Yes):
        best_yes_bid = max(yes prices)
        best_yes_ask = 1 - max(no prices)

    Prices are kept as strings (exact, no float rounding) keyed to a running
    quantity. delta_fp is the *change* in resting size at that price level.
    """

    def __init__(self, market_ticker: str) -> None:
        self.market_ticker = market_ticker
        self.yes: dict[str, float] = {}  # price_str -> qty
        self.no: dict[str, float] = {}
        self.ready = False  # True once the snapshot has been applied

    @staticmethod
    def _apply_levels(book: dict[str, float], levels: list[list[str]] | None) -> None:
        """Overwrite `book` from a snapshot's list of [price, qty] pairs."""
        book.clear()
        if levels is None:
            return

        for level in levels:
            price, qty = level[0], float(level[1])
            book[str(price)] = qty

    def apply_snapshot(self, msg: dict) -> None:
        # snapshot keys *_dollars_fp
        self._apply_levels(self.yes, msg.get("yes_dollars_fp"))
        self._apply_levels(self.no, msg.get("no_dollars_fp"))
        self.ready = True

    def apply_delta(self, msg: dict) -> None:
        side = msg["side"]
        price = str(msg.get("price_dollars", msg.get("price")))
        delta = float(msg.get("delta_fp", msg.get("delta", 0)))
        book = self.yes if side == "yes" else self.no
        new_qty = book.get(price, 0.0) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    # --- derived quantities, all denominated in Yes ---------------------

    def best_yes_bid(self):
        """(price, qty) of the highest Yes bid, or None."""
        if not self.yes:
            return None
        p = max({k: v for k, v in self.yes.items() if v >= 1}, key=float)
        return float(p), self.yes[p]

    def best_yes_ask(self):
        """(price, qty) of the lowest Yes ask, derived from the No book."""
        if not self.no:
            return None
        # Best No bid = highest No price -> tightest Yes ask = 1 - that price.
        p = max({k: v for k, v in self.no.items() if v >= 1}, key=float)
        return round(1.0 - float(p), 4), self.no[p]

    def book_imbalance(self) -> float | None:
        """Static top-of-book imbalance in [-1, 1]: +1 = all bid, -1 = all ask."""
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if not bid or not ask:
            return None
        qb, qa = bid[1], ask[1]
        total = qb + qa
        return (qb - qa) / total if total else None


class OFITracker:
    """Order-flow imbalance (Cont-Kukanov-Stoikov), event-by-event.

    Compares best bid/ask price & size between consecutive book states and
    accumulates the signed contribution of each event. Reset the window when
    you want a fresh reading (e.g. per second / per N events).
    """

    def __init__(self) -> None:
        self._prev = None  # (bid_px, bid_qty, ask_px, ask_qty)
        self.ofi = 0.0

    def update(self, book: "OrderBook") -> None:
        bid, ask = book.best_yes_bid(), book.best_yes_ask()
        if not bid or not ask:
            return
        cur = (bid[0], bid[1], ask[0], ask[1])
        if self._prev is not None:
            pbp, pbq, pap, paq = self._prev
            bp, bq, ap, aq = cur
            # Bid-side contribution
            if bp > pbp:
                self.ofi += bq
            elif bp == pbp:
                self.ofi += bq - pbq
            else:
                self.ofi -= pbq
            # Ask-side contribution (sign flipped)
            if ap < pap:
                self.ofi -= aq
            elif ap == pap:
                self.ofi -= aq - paq
            else:
                self.ofi += paq
        self._prev = cur

    def reset(self) -> float:
        val = self.ofi
        self.ofi = 0.0
        return val


REFRESH_HZ = 10  # how many times per second to repaint the status line


async def display_loop(book: "OrderBook", ofi: "OFITracker", stats: dict) -> None:
    """Repaint a single in-place status line at a fixed rate.

    Decoupled from the message rate: the book updates on every delta, but we
    only render REFRESH_HZ times per second so the terminal stays readable.
    Uses '\\r' + flush so each line overwrites the previous one (no scrolling).
    """
    period = 1.0 / REFRESH_HZ
    last_delta_count = 0
    while True:
        await asyncio.sleep(period)
        if not book.ready:
            continue

        bid, ask = book.best_yes_bid(), book.best_yes_ask()
        bid_ask_spread = int(100 * (ask[0] - bid[0]))
        imb = book.book_imbalance()
        if not (bid and ask and imb is not None):
            continue

        # Delta rate since the last repaint -> "activity" gauge.
        rate = (stats["deltas"] - last_delta_count) * REFRESH_HZ
        last_delta_count = stats["deltas"]

        last_trade = stats.get("last_trade") or "-"
        line = (
            f"bid {bid[0]:.2f}x{bid[1]:>5.0f} | ask {ask[0]:.2f}x{ask[1]:>5.0f} "
            f"| imb {imb:+.3f} | OFI {ofi.ofi:+8.1f} "
            f"| {rate:>4.0f} upd/s | last trade {last_trade} | spread {bid_ask_spread:>2.0f}   "
        )
        # Pad to a fixed width so leftovers from a longer prior line are wiped.
        print(f"\r{line:<110}", end="", flush=True)


async def handle_messages(websocket):
    """Subscribe and process messages for the life of one connection."""
    # Subscribe to both the order book and the trade (execution) feed.
    # orderbook_delta -> changes to resting limit orders (+ one snapshot on subscribe)
    # trade          -> actual executed trades (this is the "trade activity" feed)
    subscribe_msg = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "trade"],
            "market_tickers": [MARKET_TICKER],
        },
    }
    await websocket.send(json.dumps(subscribe_msg))

    book = OrderBook(MARKET_TICKER)
    ofi = OFITracker()
    stats = {"deltas": 0, "trades": 0, "last_trade": None}

    # Run the display on its own timer, independent of the message stream.
    painter = asyncio.create_task(display_loop(book, ofi, stats))

    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")
            msg = data.get("msg", {})

            if msg_type == "subscribed":
                # \n first so we don't leave the status line half-drawn above.
                print(f"Subscribed: {data}")

            elif msg_type == "orderbook_snapshot":
                book.apply_snapshot(msg)
                print(
                    f"Snapshot applied: {len(book.yes)} yes levels, "
                    f"{len(book.no)} no levels"
                )

            elif msg_type == "orderbook_delta":
                if not book.ready:
                    continue  # wait for the snapshot before applying deltas, should ne be the case as snapshot is sent first
                book.apply_delta(msg)
                ofi.update(book)
                stats["deltas"] += 1

            elif msg_type == "trade":
                stats["trades"] += 1
                # Trade msg fields (confirmed): yes_price_dollars / no_price_dollars,
                # count_fp (size), taker_side ('yes'/'no'), taker_book_side.
                price = msg.get("yes_price_dollars")
                count = float(msg.get("count_fp", 0))
                side = msg.get("taker_side", "")
                stats["last_trade"] = f"{count:.0f}@{price} {side}"

            elif msg_type == "error":
                print(f"\nError: {data}")

            else:
                print(f"\nUnhandled message type {msg_type!r}: {data}")
    finally:
        painter.cancel()


async def orderbook_websocket():
    """Connect to WebSocket and keep reconnecting if the server drops us."""
    while True:
        # Re-create headers each attempt: the signed timestamp must be fresh.
        ws_headers = create_headers(PRIVATE_KEY, "GET", "/trade-api/ws/v2")
        try:
            # ping_interval keeps the connection alive; open_timeout avoids hanging.
            async with websockets.connect(
                WS_URL,
                additional_headers=ws_headers,
                ping_interval=10,
                ping_timeout=20,
                open_timeout=10,
            ) as websocket:
                print(f"Connected! Subscribing to {MARKET_TICKER}")
                await handle_messages(websocket)

        except websockets.exceptions.ConnectionClosed as e:
            print(f"Connection closed ({e!r}); reconnecting in 3s...")
        except OSError as e:
            print(f"Network error ({e!r}); reconnecting in 3s...")

        await asyncio.sleep(3)


# Run the example
if __name__ == "__main__":
    try:
        asyncio.run(orderbook_websocket())
    except KeyboardInterrupt:
        print("\nStopped.")
