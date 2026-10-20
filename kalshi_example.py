#! /bin/python
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import websockets
from dotenv import load_dotenv

from connection_utils import create_headers, get_live_markets, get_series_list, load_private_key_from_file
from data_models import InvalidMarket, MarketState, OFITracker, OrderBook
from data_verifiers import replay_json

load_dotenv(".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)
LOCAL_WS_URL = "ws://localhost:8765"
DEFAULT_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_URL = os.getenv("KALSHI_WS_URL", DEFAULT_WS_URL)
LOG_ROOT = Path(os.getenv("KALSHI_DATA_DIR", "data_test" if WS_URL != DEFAULT_WS_URL else "data"))
NY = ZoneInfo("America/New_York")
PAINTER_RUNNING = False
method = "GET"

REFRESH_HZ = 1  # how many times per second to repaint the status line

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
        prog="kalshi_example", description="Driver script for reading kalshi orderbook data, logging from the orderbook websocket, and storing it to disc"
    )

    parser.add_argument("--log", action="store_true", help="Log incoming messages")  # todo #10

    # Optional flags automatically parse and store the value following the flag name
    parser.add_argument("--file", type=str, help="<Path> to read messages from")
    parser.add_argument("--category", type=str, help="Kalshi series category to subscribe to")
    parser.add_argument("--series", type=str, help="Kalshi series ticker to subscribe to")
    parser.add_argument("--volume", type=float, default=0.0, help="Minimum volume for a series (only used with --category)")
    parser.add_argument("--tickers", nargs="+", default=[], help="List of kalshi tickers to subscribe to")
    # todo #9

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
    if args.tickers:
        LOGGER.info(f"Subscribing to tickers {', '.join(args.tickers)}")

    return args


def log_path(ts_ns: int) -> Path:
    t = datetime.fromtimestamp(ts_ns / 1e9, tz=NY)
    return LOG_ROOT / t.strftime("%Y%m%d") / f"orderbook_{t.strftime('%H')}.txt"


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
                f, current = open(path, "a"), path  # noqa: ASYNC230, SIM115
            assert f is not None
            f.write(json.dumps({"timestamp": ts, "msg": raw}) + "\n")
            if queue.empty():
                f.flush()
    finally:
        if f:
            f.close()


def log_message(msg: str, file: TextIO) -> None:
    stamped_msg = {"timestamp": time.time_ns(), "msg": msg}
    file.write(f"{json.dumps(stamped_msg)}\n")


def classify_message(data: Any, msg: dict[str, str | int], msg_type: str, *, book: OrderBook, ofi: OFITracker, stats: dict[str, float | None]) -> None:
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
        stats["last_trade"] = price  # f"{count:.0f}@{price} {side}"

    elif msg_type == "error":
        LOGGER.warning(f"\nError: {data}")

    else:
        LOGGER.warning(f"\nUnhandled message type {msg_type!r}: {data}")


async def display_loop(states: dict[str, MarketState], status: dict[str, str]) -> None:
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
        lines = [f"[{status['text']}]"]  # constant-height header line
        for ticker, s in states.items():
            delta = s.stats["deltas"]
            if delta:
                rate = max(0.0, delta - last_deltas[ticker]) * REFRESH_HZ  # counters restart at 0 on reconnect
                last_deltas[ticker] = delta
            else:
                continue
            imb = s.book.book_imbalance() if s.book.ready else None
            if imb is None:
                lines.append(f"{ticker:<40} | waiting for two-sided book")
            else:
                lines.append(f"{ticker:<40} | imb {imb:+.3f} | OFI {s.ofi.ofi:+8.1f} | {rate:>4.0f} upd/s")

        # \033[K clears to end of line so a shorter line doesn't leave leftovers.
        frame = "\n".join(line + "\033[K" for line in lines)
        if not first_frame:
            sys.stdout.write(f"\033[{len(lines)}A")  # cursor up over the last frame
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        first_frame = False


async def subscribe_to_ws(websocket, tickers: list[str]):
    subscribe_msg = {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta", "trade"], "market_tickers": [string.upper() for string in tickers]}}
    await websocket.send(json.dumps(subscribe_msg))


def new_state(t: str) -> MarketState:
    return MarketState(ticker=t, book=OrderBook(t), ofi=OFITracker(), stats={"deltas": 0.0, "trades": 0.0, "last_trade": None})


async def error_data_loop(errors: Counter, queue: asyncio.Queue):
    while True:
        await asyncio.sleep(60)
        LOGGER.info("errors=%s queue_size=%d", dict(errors), queue.qsize())


async def handle_messages(websocket, tickers: list[str], states: dict[str, MarketState], queue: asyncio.Queue, errors: Counter) -> None:
    """Subscribe, then route frames to per-market state until the connection ends."""
    await subscribe_to_ws(websocket, tickers)
    unrouted: Counter = Counter()

    async for message in websocket:
        try:
            queue.put_nowait((time.time_ns(), message))  # log first, whatever happens next
        except asyncio.QueueFull:  # should never occur as queue size is infinite
            LOGGER.warning("Queue is full, cannot add message of type %s", json.loads(message).get("type", "NONE"))
            await queue.put((time.time_ns(), message))

        try:
            data = json.loads(message)
            msg_type = data.get("type")
            msg = data.get("msg", {})

            if msg_type == "subscribed":
                LOGGER.info("Subscribed: channel=%s sid=%s", msg.get("channel"), msg.get("sid"))
                continue

            if msg_type == "error":
                LOGGER.warning("Server error: %s", data)
                continue

            ticker = msg.get("market_ticker")
            state = states.get(ticker)
            if state is None:
                unrouted[ticker] += 1
                if unrouted[ticker] == 1:  # warn once per unexpected ticker
                    LOGGER.warning("Frame for unsubscribed ticker %r (type=%s)", ticker, msg_type)
                continue

            classify_message(data, msg, msg_type, book=state.book, ofi=state.ofi, stats=state.stats)

        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError) as e:
            errors[type(e).__name__] += 1
            if errors[type(e).__name__] <= 5:
                LOGGER.exception("Bad frame, skipping: %.200r", message)


def writer_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        LOGGER.error("writer task died: %r", exc)
        sys.exit(1)


async def orderbook_websocket(tickers: list[str]):
    tickers = [t.upper() for t in tickers]
    states = {t: new_state(t) for t in tickers}  # lives for the whole process
    status = {"text": "connecting"}  # shown on a header line
    queue: asyncio.Queue = asyncio.Queue()
    errors: Counter = Counter()
    writer = asyncio.create_task(writer_task(queue), name="writer")
    painter = asyncio.create_task(display_loop(states, status), name="painter")
    error_reporter = asyncio.create_task(error_data_loop(errors, queue))
    writer.add_done_callback(writer_done)
    try:
        while True:
            if not KALSHI_ACCESS_KEY:
                raise ValueError(f"KALSHI_ACCESS_KEY cannont be `{type(KALSHI_ACCESS_KEY)}`")
            ws_headers = create_headers(PRIVATE_KEY, "GET", "/trade-api/ws/v2", KALSHI_ACCESS_KEY)
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=ws_headers, ping_interval=10, ping_timeout=20, open_timeout=10
                ) as websocket:  # this uses deflate compression; switch to none?
                    for t in tickers:  # fresh books, SAME dict object
                        states[t] = new_state(t)
                    status["text"] = "connected"
                    await handle_messages(websocket, tickers, states, queue, errors)
                reason = "server closed the connection"
            except websockets.exceptions.ConnectionClosed as e:
                reason = f"Connection closed ({e!r})"
                errors["ConnectionClosed"] += 1
            except OSError as e:
                reason = f"Network error ({e!r})"
                errors[type(e).__name__] += 1
            except Exception:
                LOGGER.exception("Unexpected error in connection loop")
                reason = "unexpected error"
                errors["unexpected"] += 1

            status["text"] = f"{reason}, retrying in 3s"
            LOGGER.warning("%s, reconnection in 3 seconds", reason)
            await asyncio.sleep(3)
    finally:
        for thing in (painter, writer, error_reporter):
            thing.cancel()


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
    elif args.tickers:
        markets = {match.group(1): [t for t in args.tickers if t.startswith(match.group(1))] for s in args.tickers if (match := re.search(r"^(\w+)(?=-)", s))}
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
