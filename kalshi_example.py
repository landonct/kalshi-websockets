#! /bin/python
import asyncio
import json
import os
import time
from _io import TextIOWrapper
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets
from dotenv import load_dotenv

from connection_utils import create_headers, load_private_key_from_file
from data_models import OFITracker, OrderBook

load_dotenv(".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
MARKET_TICKER = "KXUSLGAME-26SEP25PASTUL-PAS"  # KXFEDDECISION-26SEP-H25" KXMLBGAME-26AUG191235DETPIT-DET
LOG_FILE = Path("test.txt")

method = "GET"

REFRESH_HZ = 10  # how many times per second to repaint the status line

def log_message(msg: str, file: TextIOWrapper) -> None:
    stamped_msg = {"timestamp": time.time_ns(), "msg": msg}
    file.write(f"{json.dumps(stamped_msg)}\n")


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
        if ask is None or bid is None:
            raise TypeError("ask or bid cannot be None")
        else:
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


async def handle_messages(websocket, file: Path):
    """Subscribe and process messages for the life of one connection."""
    # Subscribe to both the order book and the trade (execution) feed.
    # orderbook_delta -> changes to resting limit orders (+ one snapshot on subscribe)
    # trade          -> actual executed trades (this is the "trade activity" feed)
    subscribe_msg = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "trade"],
            "market_tickers": [MARKET_TICKER.upper()],
        },
    }
    await websocket.send(json.dumps(subscribe_msg))

    book = OrderBook(MARKET_TICKER.upper())
    ofi = OFITracker()
    stats = {"deltas": 0, "trades": 0, "last_trade": None}

    # Run the display on its own timer, independent of the message stream.
    painter = asyncio.create_task(display_loop(book, ofi, stats))

    try:
        # we want this to be a buffered write, ignore the linters sugestions
        with open(file, "a") as f:  # noqa: ASYNC230
            async for message in websocket:
                log_message(message, f)
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
        assert KALSHI_ACCESS_KEY is not None
        ws_headers = create_headers(
            PRIVATE_KEY, "GET", "/trade-api/ws/v2", KALSHI_ACCESS_KEY
        )
        try:
            # ping_interval keeps the connection alive; open_timeout avoids hanging.
            async with websockets.connect(
                WS_URL,
                additional_headers=ws_headers,
                ping_interval=10,
                ping_timeout=20,
                open_timeout=10,
            ) as websocket:
                print(f"Connected! Subscribing to {MARKET_TICKER.upper()}")
                log_file = MARKET_TICKER + "_" + datetime.now(tz=ZoneInfo("America/New_York")).strftime("%d%b%Y_%H") + ".txt"
                await handle_messages(websocket, log_file)

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
