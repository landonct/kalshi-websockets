#! /bin/python
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
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
from data_verifiers import replay_json

load_dotenv(".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)
KALSHI_WS_URL="ws://localhost:8765"
WS_URL = os.getenv("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2")
LOG_ROOT = Path("data")
NY = ZoneInfo("America/New_York")
method = "GET"

REFRESH_HZ = 10  # how many times per second to repaint the status line

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=f"logs/kalshi_example_{datetime.now(tz=ZoneInfo('America/New_York')).strftime('%Y%m%d')}.log",
    filemode="a",  # 'w' overwrites the file; 'a' appends (default)
)
LOGGER = logging.getLogger()

if WS_URL != "wss://external-api-ws.kalshi.com/trade-api/ws/v2":
    LOGGER.warning("Using NON-DEFAULT websocket URL: %s", WS_URL)
    print(f"WARNING: connecting to {WS_URL}, not Kalshi")


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
    parser.add_argument("--volume", type=float, default=0.0, help="Minimum volume for a series (only used with --category)")

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

def log_path(ts_ns: int) -> Path:
    t = datetime.fromtimestamp(ts_ns / 1e9, tz=NY)
    return LOG_ROOT / t.strftime("%Y%m%d") / f"orderbook_{t.strftime("%H")}.txt"

async def writer_task(queue: asyncio.Queue) -> None:
    f, current = None, None
    try:
        while True:
            ts, raw = await queue.get()
            path = log_path(ts)
            if path != current:
                if f:
                    f.close()
                path.parent.mkdir(parents=True, exist_ok=True)
                f, current = open(path, "a"), path
            assert f is not None
            f.write(json.dumps({"timestamp": ts, "msg": raw}) + '\n')
            if queue.empty():
                f.flush()
    finally:
        if f:
            f.close()

def log_message(msg: str, file: TextIO) -> None:
    stamped_msg = {"timestamp": time.time_ns(), "msg": msg}
    file.write(f"{json.dumps(stamped_msg)}\n")


def classify_message(
    data: Any,
    msg: dict[str, str | int],
    msg_type: str,
    *,
    book: OrderBook,
    ofi: OFITracker,
    stats: dict[str, float | None],
) -> None:
    if msg_type == "subscribed":
        # \n first so we don't leave the status line half-drawn above.
        LOGGER.info(f"Subscribed: {data}")

    elif msg_type == "orderbook_snapshot":
        book.apply_snapshot(msg)
        LOGGER.info(f"Snapshot applied: {len(book.yes)} yes levels, {len(book.no)} no levels")

    elif msg_type == "orderbook_delta":
        if not book.ready:
            return  # wait for the snapshot before applying deltas, should ne be the case as snapshot is sent first
        book.apply_delta(msg)
        ofi.update(book)
        if stats["deltas"] is not None:
            stats["deltas"] += 1 
        else:
            stats["deltas"] = 1

    elif msg_type == "trade":
        if stats["trades"] is not None:
            stats["trades"] += 1
        else:
            stats["trades"] = 1
        # Trade msg fields (confirmed): yes_price_dollars / no_price_dollars,
        # count_fp (size), taker_side ('yes'/'no'), taker_book_side.
        price = float(msg.get("yes_price_dollars", -1.0))
        # count = float(msg.get("count_fp", 0))
        # side = msg.get("taker_side", "")
        stats["last_trade"] = price # f"{count:.0f}@{price} {side}"

    elif msg_type == "error":
        LOGGER.warning(f"\nError: {data}")

    else:
        LOGGER.warning(f"\nUnhandled message type {msg_type!r}: {data}")


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
            delta = s.stats["deltas"] if s.stats["deltas"] is not None else 0.0
            rate = (delta - last_deltas[ticker]) * REFRESH_HZ
            last_deltas[ticker] = delta

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


async def handle_messages(websocket, tickers, queue: asyncio.Queue):
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
    try:
        async for message in websocket:
            queue.put_nowait((time.time_ns(), message))
            data = json.loads(message)
            msg_type = data.get("type")
            msg = data.get("msg", {})
            
            state = states.get(msg.get("market_ticker"))
            if state is None:
                LOGGER.info("control message: %s", data)
                continue
            # todo #7
            classify_message(data, msg, msg_type, book=state.book, ofi=state.ofi, stats=state.stats)
    finally:
        painter.cancel()


async def orderbook_websocket(tickers:list[str]):
    """Connect to WebSocket and keep reconnecting if the server drops us."""
    queue: asyncio.Queue = asyncio.Queue()
    writer = asyncio.create_task(writer_task(queue))
    try:
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
                    LOGGER.info(f"Connected! Subscribing to {len(tickers)} markets")
                    await handle_messages(websocket, tickers, queue)

            except websockets.exceptions.ConnectionClosed as e:
                LOGGER.warning(f"Connection closed ({e!r}); reconnecting in 3s...")
            except OSError as e:
                LOGGER.warning(f"Network error ({e!r}); reconnecting in 3s...")

            await asyncio.sleep(3)
    finally:
        writer.cancel()

def main():
    args = parse_args()
    if args.file:
        book = OrderBook(market_ticker="replay")
        replay_json(args.file, book)
        return
    if bool(args.category):
        series = get_series_list(args.category, bool(args.volume), args.volume)
        series = series[series["tickers"].str.contains(r"GAME|MATCH")]
        markets = {series_ticker: get_live_markets(series_ticker) for series_ticker in series["tickers"]}
    elif args.series is not None:
        markets = {args.series: get_live_markets(args.series)} 
    else:
        print("usage: kalshi_example [-h] [--log] [--file FILE] [--category CATEGORY] [--series SERIES] [--volume VOLUME]")
        raise InvalidMarket("MARKET cannot be None")
    
    all_tickers = [ticker for tickers in markets.values() for ticker in tickers]
    print(f"Subscribing to {len(all_tickers)} markets across {len(markets)} series")
    try:
        asyncio.run(orderbook_websocket(all_tickers))
    except KeyboardInterrupt:
        print("\nStopped.")

# Run the example
if __name__ == "__main__":
    # book = OrderBook("something")
    # book = replay_json(Path("test.txt"), book)
    main()
