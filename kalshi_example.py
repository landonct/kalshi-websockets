#! /bin/python
import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import requests
import websockets
from dotenv import load_dotenv

from connection_utils import create_headers, fetch_markets, get_live_markets, get_series_list, load_private_key_from_file
from data_models import InvalidMarket, MarketState, OFITracker, OrderBook
from data_verifiers import replay_json

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = load_private_key_from_file(PRIVATE_KEY_PATH)
LOCAL_WS_URL = "ws://localhost:8765"
DEFAULT_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_URL = os.getenv("KALSHI_WS_URL", DEFAULT_WS_URL)
DATA_ROOT = Path(os.getenv("KALSHI_DATA_DIR", f"{ROOT_DIR}/data_test" if WS_URL != DEFAULT_WS_URL else f"{ROOT_DIR}/data"))
NY = ZoneInfo("America/New_York")
PAINTER_RUNNING = False
TIME_OFFSET = 0
FLUSH_TIME = 1.0
method = "GET"

REFRESH_HZ = 1  # how many times per second to repaint the status line

Path(f"{ROOT_DIR}/logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename=f"{ROOT_DIR}/logs/kalshi_example_{datetime.now(tz=ZoneInfo('America/New_York')).strftime('%Y%m%d')}.log",
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
    parser.add_argument("--ack-wait", type=int, default=15, help="Maximum amount of time to wait for a 'subscribed' acknowledgement from the server")
    parser.add_argument("--flush-time", type=float, dest="FLUSH_TIME", default=1.0)
    # todo #9

    args = parser.parse_args()
    if args.file is not None and not Path(f"{ROOT_DIR}/{args.file}").is_file():
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


def _verify_env() -> None:
    root = Path(__file__).resolve().parent
    if not KALSHI_ACCESS_KEY:
        print("No value for KALSHI_ACCESS_KEY found", file=sys.stderr)
        sys.exit(4)
    if not PRIVATE_KEY_PATH:
        print("No value for PRIVATE_KEY_PATH found", file=sys.stderr)
        sys.exit(4)
    if not (root / PRIVATE_KEY_PATH).is_file():
        print("Cannot find file pointed to by PRIVATE_KEY_PATH", file=sys.stderr)
        sys.exit(5)


def _data_path(ts_ns: int) -> Path:
    t = datetime.fromtimestamp((ts_ns + TIME_OFFSET) / 1e9, tz=NY)
    return DATA_ROOT / t.strftime("%Y%m%d") / f"orderbook_{t.strftime('%H')}.txt"


def _force_rotation(signum: int, frame):
    global TIME_OFFSET
    TIME_OFFSET += int(3600 * 1e9)
    LOGGER.warning("Test hook: Moving timestamp an hour forward to crash writer")


def _write_pid():
    pid = os.getpid()
    with open(ROOT_DIR / "pid.txt", "w", encoding="utf-8") as f:
        f.write(str(pid))


signal.signal(signal.SIGUSR1, _force_rotation)


async def writer_task(queue: asyncio.Queue, written: Counter) -> None:
    f: TextIO | None = None
    current: Path | None = None
    last_flush = time.monotonic()
    try:
        while True:
            try:
                ts, raw = await asyncio.wait_for(queue.get(), timeout=FLUSH_TIME)
            except asyncio.TimeoutError:
                if f:
                    f.flush()
                    last_flush = time.monotonic()
                continue
            path = _data_path(ts)
            if f is None or path != current:
                if f:
                    f.close()
                path.parent.mkdir(parents=True, exist_ok=True)
                f = open(path, "a")  # noqa: ASYNC230, SIM115
                current = path
            f.write(json.dumps({"timestamp": ts, "msg": raw}) + "\n")
            written["written"] += 1
            if time.monotonic() - last_flush >= FLUSH_TIME:
                f.flush()
                last_flush = time.monotonic()
    finally:
        if f:
            f.close()


def log_message(msg: str, file: TextIO) -> None:
    stamped_msg = {"timestamp": time.time_ns(), "msg": msg}
    file.write(f"{json.dumps(stamped_msg)}\n")


def classify_message(data: Any, msg: dict[str, str | int], msg_type: str, *, book: OrderBook, ofi: OFITracker, stats: dict[str, float | None]) -> None:
    if msg_type == "subscribed":
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
        price = msg.get("yes_price_dollars")
        if price:
            price = float(str(price))
        else:
            LOGGER.warning(f"No price found for trade: {msg}")
            return
        # count = float(msg.get("count_fp", 0))
        # side = msg.get("taker_side", "")
        stats["last_trade"] = price  # f"{count:.0f}@{price} {side}"

    elif msg_type == "error":
        LOGGER.warning(f"\nError: {data}")

    else:
        LOGGER.warning(f"\nUnhandled message type {msg_type!r}: {data}")


async def _reset_ofi(states: dict[str, MarketState]):
    {state.ofi.reset() for state in states.values()}


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
            try:
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
            except Exception:
                LOGGER.exception(f"ticker {ticker} failed to render in the painter")
                lines.append(f"{ticker:<40} | display error")

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


async def monitor_loop(errors: Counter, frames: Counter, written: Counter, queue: asyncio.Queue, *, loop_interval: int = 60):
    while True:
        await asyncio.sleep(loop_interval)
        LOGGER.info("errors=%s frames=%s written=%s queue_size=%d", dict(errors), dict(frames), dict(written), queue.qsize())


async def handle_messages(websocket, tickers: list[str], states: dict[str, MarketState], queue: asyncio.Queue, errors: Counter, frames: Counter, *, ack_wait: int = 5) -> None:
    """Subscribe, then route frames to per-market state until the connection ends."""
    await subscribe_to_ws(websocket, tickers)
    pending_channels = {"orderbook_delta", "trade"}
    unrouted: Counter = Counter()

    while True:
        timeout = ack_wait if pending_channels else None
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"Timed out waiting for a subscription ack(s): {pending_channels}") from e
        except websockets.exceptions.ConnectionClosedOK:
            return

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
                frames["subscribed"] += 1
                channel = msg.get("channel")
                pending_channels.discard(channel)
                LOGGER.info("Subscribed: channel=%s sid=%s", channel, msg.get("sid"))
                continue

            if msg_type == "error":
                frames["error"] += 1
                LOGGER.warning("Server error: %s", data)
                continue

            ticker = msg.get("market_ticker")
            state = states.get(ticker)
            if state is None:
                unrouted[ticker] += 1
                if unrouted[ticker] == 1:  # warn once per unexpected ticker
                    LOGGER.warning("Frame for unsubscribed ticker %r (type=%s)", ticker, msg_type)
                continue

            frames[ticker] += 1
            classify_message(data, msg, msg_type, book=state.book, ofi=state.ofi, stats=state.stats)

        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError) as e:
            errors[type(e).__name__] += 1
            if errors[type(e).__name__] <= 5:
                LOGGER.exception("Bad frame, skipping: %.200r", message)


async def supervised_writer(queue: asyncio.Queue, written: Counter, *, restart_timer: int = 1, max_retries: int = 5):
    cur_retries = 0
    while True:
        try:
            await writer_task(queue, written)
            cur_retries = 0
        except Exception:
            cur_retries += 1
            LOGGER.exception("writer died, restarting in %d seconds. (%d/%d) retries", restart_timer, cur_retries, max_retries)
            await asyncio.sleep(restart_timer)
            if cur_retries >= max_retries:
                LOGGER.critical("Writer died and could not be restarted. Terminating the program")  # todo #12
                sys.exit(1)


async def supervised_painter(states: dict[str, MarketState], status: dict[str, str], restart_timer: int = 1, max_retries: int = 5):
    cur_retries = 0
    while True:
        try:
            await display_loop(states, status)
            cur_retries = 0
        except Exception:
            cur_retries += 1
            LOGGER.exception("painter died, restarting in %d seconds. (%d/%d) retries", restart_timer, cur_retries, max_retries)
            await asyncio.sleep(restart_timer)
            if cur_retries >= max_retries:
                LOGGER.error("painter died and could not be restarted. continuing without the painter")


async def every(interval: float, fn, args: dict[str, Any], name: str) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            fn(**args)
        except Exception:
            LOGGER.exception("periodic job %s failed", name)


async def orderbook_websocket(tickers: list[str], ack_wait: int, ofi_interval: int = 10):
    retry_wait = 3
    max_retries = 5
    tickers = [t.upper() for t in tickers]
    states = {t: new_state(t) for t in tickers}  # lives for the whole process
    status = {"text": "connecting"}  # shown on a header line
    queue: asyncio.Queue = asyncio.Queue()
    errors: Counter = Counter()
    frames: Counter = Counter()
    written: Counter = Counter()
    ofi_refresher = asyncio.create_task(every(ofi_interval, _reset_ofi, args={"states": states}, name="ofi"))
    writer = asyncio.create_task(supervised_writer(queue, written), name="writer")
    painter = asyncio.create_task(supervised_painter(states, status), name="painter")
    error_reporter = asyncio.create_task(monitor_loop(errors, frames, written, queue), name="monitor")
    try:
        while True:
            if not KALSHI_ACCESS_KEY:
                raise ValueError(f"KALSHI_ACCESS_KEY cannont be `{type(KALSHI_ACCESS_KEY)}`")
            ws_headers = create_headers(PRIVATE_KEY, "GET", "/trade-api/ws/v2", KALSHI_ACCESS_KEY)
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=ws_headers, ping_interval=10, ping_timeout=20, open_timeout=10, proxy=True if WS_URL == DEFAULT_WS_URL else None
                ) as websocket:  # this uses deflate compression; switch to none?
                    for t in tickers:  # fresh books, SAME dict object
                        states[t] = new_state(t)
                    status["text"] = "connected"
                    await handle_messages(websocket, tickers, states, queue, errors, frames)
                reason = "server closed the connection"
            except websockets.exceptions.ConnectionClosed as e:
                reason = f"Connection closed ({e!r})"
                errors["ConnectionClosed"] += 1
            except TimeoutError as e:
                reason = f"Connection timed out({e!r})"
                errors[type(e).__name__] += 1
                if errors[type(e).__name__] >= max_retries:
                    LOGGER.critical("Connection continued to timeout after %d retries. Terminating the program", max_retries)
                    sys.exit(2)
            except OSError as e:
                reason = f"Network error ({e!r})"
                errors[type(e).__name__] += 1
            except websockets.exceptions.InvalidStatus as e:
                LOGGER.exception("Connection failed")
                reason = f"Connection failed ({e!r:.{shutil.get_terminal_size().columns - 40}})"
                errors[type(e).__name__] += 1
                retry_wait = 2 ** (errors[type(e).__name__] - 1)
                if errors[type(e).__name__] >= max_retries:
                    LOGGER.critical("Connection could not be restored after %d retries. Terminating the program", max_retries)
                    sys.exit(3)
            except Exception:
                LOGGER.exception("Unexpected error in connection loop")
                reason = "unexpected error"
                errors["unexpected"] += 1

            status["text"] = f"{reason}, retrying in {retry_wait}s"
            LOGGER.warning("%s, reconnecting in %d seconds", reason, retry_wait)
            await asyncio.sleep(retry_wait)
    finally:
        for thing in (painter, writer, error_reporter, ofi_refresher):
            thing.cancel()


def main():
    _verify_env()
    _write_pid()
    args = parse_args()
    global FLUSH_TIME
    FLUSH_TIME = args.FLUSH_TIME
    if args.file:
        book = OrderBook(market_ticker="replay")
        replay_json(args.file, book)
        return
    if bool(args.category):
        series = get_series_list(args.category, bool(args.volume), args.volume)
        series = series[series["tickers"].str.contains(r"GAME|MATCH")]
        markets = fetch_markets(series["tickers"])
    elif args.series is not None:
        try:
            value = get_live_markets(args.series)
        except requests.RequestException:
            LOGGER.critical(f"Series {args.series} could not be found at {WS_URL}. Terminating the program")
            sys.exit(7)
        markets = {args.series: value}
    elif args.tickers:
        markets = {match.group(1): [t for t in args.tickers if t.startswith(match.group(1))] for s in args.tickers if (match := re.search(r"^(\w+)(?=-)", s))}
    else:
        print("usage: kalshi_example [-h] [--log] [--file FILE] [--category CATEGORY] [--series SERIES] [--volume VOLUME]")
        raise InvalidMarket("MARKET cannot be None")

    all_tickers = [ticker for tickers in markets.values() for ticker in tickers]
    print(f"Subscribing to {len(all_tickers)} markets across {len(markets)} series")
    try:
        asyncio.run(orderbook_websocket(all_tickers, args.ack_wait))
    except KeyboardInterrupt:
        print("\nStopped.")


# Run the example
if __name__ == "__main__":
    # book = OrderBook("something")
    # book = replay_json(Path("test.txt"), book)
    main()
