#! /bin/python
import sys
import _asyncio
import argparse
import asyncio
import contextlib
import json
import logging
import os
import time
from _io import TextIOWrapper
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import websockets
from dotenv import load_dotenv

from connection_utils import (
    create_headers,
    get_live_markets,
    get_series_list,
    load_private_key_from_file,
)
from data_models import InvalidMarket, MarketState, OFITracker, OrderBook

load_dotenv(".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
MARKET_TICKER = "KXNCAAFGAME-26SEP26MISSFLA-MISS"  # KXFEDDECISION-26SEP-H25" KXMLBGAME-26AUG191235DETPIT-DET
LOG_FILE = Path("test.txt")

method = "GET"

REFRESH_HZ = 10  # how many times per second to repaint the status line

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=f"kalshi_example_{datetime.now(tz=ZoneInfo('America/New_York')).strftime('%Y%m%d')}.log",
    filemode="w",  # 'w' overwrites the file; 'a' appends (default)
)
LOGGER = logging.getLogger()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kalshi_example",
        description="Driver script for reading kalshi orderbook data, logging from the orderbook websocket, and storing it to disc",
    )

    parser.add_argument("--log", action="store_true", help="Log incoming messages")
    
    # Optional flags automatically parse and store the value following the flag name
    parser.add_argument("--file", type=str, help="<Path> to read messages from")
    parser.add_argument("--category", type=str, help="Kalshi series category to subscribe to")
    parser.add_argument("--series", type=str, help="Kalshi series ticker to subscribe to")
    parser.add_argument("--volume", type=float, default=0.0, help="Minimum volume for a series")

    args = parser.parse_args()
    if args.file is not None and not Path(args.file).is_file():
        raise FileNotFoundError(f"{args.file} does not exist.")
    if args.log:
        LOGGER.info("Logging mode enabled")
    if args.file:
        LOGGER.info(f"Reading from file {args.file}")
    if args.category:
        LOGGER.info(f"Subscribing to markets with category {args.category}")
        if args.volume:
            LOGGER.info(f"Only considering markets with volume > {args.volume}")
    if args.series:
        LOGGER.info(f"Subscribing to market {args.series}")

    return args


def log_message(msg: str, file: TextIOWrapper) -> None:
    stamped_msg = {"timestamp": time.time_ns(), "msg": msg}
    file.write(f"{json.dumps(stamped_msg)}\n")


def classify_message(
    data: Any,
    msg: str,
    msg_type: str,
    *,
    book: OrderBook,
    ofi: OFITracker,
    stats: dict[str, float | None],
) -> None:
    if msg_type == "subscribed":
        # \n first so we don't leave the status line half-drawn above.
        print(f"Subscribed: {data}")

    elif msg_type == "orderbook_snapshot":
        book.apply_snapshot(msg)
        print(f"Snapshot applied: {len(book.yes)} yes levels, {len(book.no)} no levels")

    elif msg_type == "orderbook_delta":
        if not book.ready:
            return  # wait for the snapshot before applying deltas, should ne be the case as snapshot is sent first
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


async def display_loop(states: dict[str, MarketState]) -> None:
    """Repaint a single in-place status line at a fixed rate.

    Decoupled from the message rate: the book updates on every delta, but we
    only render REFRESH_HZ times per second so the terminal stays readable.
    Uses '\\r' + flush so each line overwrites the previous one (no scrolling).
    """
    period = 1.0 / REFRESH_HZ
    last_deltas = {t: 0.0 for t in states}
    first_frame = True

    while True:
        await asyncio.sleep(period)

        lines = []
        for ticker, s in states.items():
            rate = (s.stats["deltas"] - last_deltas[ticker]) * REFRESH_HZ
            last_deltas[ticker] = s.stats["deltas"]

            imb = s.book.book_imbalance() if s.book.ready else None
            if imb is None:
                lines.append(f"{ticker:<40} | waiting for two-sided book")
            else:
                lines.append(
                    f"{ticker:<40} | imb {imb:+.3f} | OFI {s.ofi.ofi:+8.1f} | {rate:>4.0f} upd/s"
                )

        # \033[K clears to end of line so a shorter line doesn't leave leftovers.
        frame = "\n".join(line + "\033[K" for line in lines)
        if not first_frame:
            sys.stdout.write(f"\033[{len(lines)}A")  # cursor up over the last frame
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        first_frame = False


async def subscribe_to_ws(websocket, tickers: list[str]):
    subscribe_msg = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta", "trade"],
            "market_tickers": [string.upper() for string in tickers],
        },
    }
    await websocket.send(json.dumps(subscribe_msg))


async def handle_messages(websocket, tickers, file: list[Path], from_file: bool):
    """Subscribe and process messages for the life of one connection."""
    # Subscribe to both the order book and the trade (execution) feed.
    # orderbook_delta -> changes to resting limit orders (+ one snapshot on subscribe)
    # trade          -> actual executed trades (this is the "trade activity" feed)
    await subscribe_to_ws(websocket, tickers)

    states = {
        t.upper(): MarketState(
            ticker = t.upper(),
            book = OrderBook(t.upper()),
            ofi = OFITracker(),
            stats = {"deltas": 0.0, "trades": 0.0, "last_trade": None}
        )
        for t in tickers
    }

    # Run the display on its own timer, independent of the message stream.
    painter = asyncio.create_task(display_loop(states))

    context = open(file, "a") if from_file else contextlib.nullcontext()  # todo #8

    try:
        # we want this to be a buffered write, ignore the linters sugestions
        with context as f:
            async for message in websocket:
                log_message(message, f)
                data = json.loads(message)
                msg_type = data.get("type")
                msg = data.get("msg", {})

                # todo #7
                classify_message(data, msg, msg_type, book=book, ofi=ofi, stats=stats)
    finally:
        painter.cancel()


async def orderbook_websocket(tickers: dict[str, list[str]], from_file: bool):
    """Connect to WebSocket and keep reconnecting if the server drops us."""
    while True:
        # Re-create headers each attempt: the signed timestamp must be fresh.
        assert KALSHI_ACCESS_KEY is not None
        ws_headers = create_headers(
            PRIVATE_KEY, "GET", "/trade-api/ws/v2", KALSHI_ACCESS_KEY
        )
        if from_file:
            pass
        else:
            try:
                # ping_interval keeps the connection alive; open_timeout avoids hanging.
                async with websockets.connect(
                    WS_URL,
                    additional_headers=ws_headers,
                    ping_interval=10,
                    ping_timeout=20,
                    open_timeout=10,
                ) as websocket:
                    print(f"Connected! Subscribing to {tickers}")
                    log_files = [
                        Path(ticker
                        + "_"
                        + datetime.now(tz=ZoneInfo("America/New_York")).strftime(
                            "%-d%b%Y_%H"
                        )
                        + ".txt")
                        for ticker in tickers
                    ]
                    await handle_messages(websocket, tickers, log_files, from_file)

            except websockets.exceptions.ConnectionClosed as e:
                print(f"Connection closed ({e!r}); reconnecting in 3s...")
            except OSError as e:
                print(f"Network error ({e!r}); reconnecting in 3s...")

            await asyncio.sleep(3)


# Run the example
if __name__ == "__main__":
    args = parse_args()
    if bool(args.category):
        series = get_series_list(args.category, bool(args.volume), args.volume)
        series = series[series["tickers"].str.contains(r"GAME|MATCH")]
        markets = {series_ticker: get_live_markets(series_ticker) for series_ticker in series["tickers"]}
    elif args.series is not None:
        markets = {args.series: get_live_markets(args.series)} 
    else:
        print("usage: kalshi_example [-h] [--log] [--file FILE] [--category CATEGORY] [--series SERIES] [--volume VOLUME]")
        raise InvalidMarket("MARKET cannot be None")
    try:
        asyncio.run(orderbook_websocket(markets, from_file=(args.file is not None)))
    except KeyboardInterrupt:
        print("\nStopped.")
