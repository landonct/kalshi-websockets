# Kalshi Order Book Collector

Connects to Kalshi's WebSocket API, subscribes to the order book and trade
feeds for many markets over a **single connection**, maintains a live local
order book per market, shows a one-line-per-market live display, and logs every
raw message to disk for later research.

**Status: work in progress.** The live collector works end to end. Crash
hardening, reconnect behaviour, and scheduling are still being built (see
[Known limitations](#known-limitations)).

## Setup

Requires Python 3.12 (the code uses nested quotes inside f-strings).

Packages: `websockets` (recent version, the new asyncio client),
`python-dotenv`, `requests`, `pandas`, `numpy`, `cryptography`.

Create a `.env` file in the directory you run from:

```
KALSHI_ACCESS_KEY=<your key id>
PRIVATE_KEY_PATH=/path/to/your/private_key.pem
```

The private key is an RSA key used to sign the WebSocket handshake
(RSA-PSS / SHA-256).

## Usage

```bash
# All open markets in one series
python kalshi_example.py --series KXMLBGAME

# All series in a category whose ticker contains GAME or MATCH,
# filtered by minimum series volume
python kalshi_example.py --category Sports --volume 1000000

# Replay a previously logged file through an order book (no network)
python kalshi_example.py --file data/20261007/orderbook_20.txt
```

| Flag | Meaning |
|------|---------|
| `--series TICKER` | Subscribe to the open markets of one series. |
| `--category NAME` | Look up series in a category, keep those with `GAME` or `MATCH` in the ticker, then fetch their open markets. |
| `--volume N` | Minimum series volume. Only used with `--category`. |
| `--file PATH` | Replay a log file instead of connecting. |
| `--log` | Currently only writes a line to the app log. Raw messages are always written to `data/` regardless. |

Note: a **series** (e.g. `KXMLBGAME`) contains many events, each with its own
**markets** (e.g. `KXMLBGAME-26OCT072000TBNYY-TB`). Subscriptions are made per
market, so the series is expanded into its market tickers at startup.

## What you see

One line per market, repainted in place at `REFRESH_HZ` (10) times per second:

```
KXMLBGAME-26OCT072000TBNYY-TB            | imb -0.323 | OFI +126500.0 |   10 upd/s
```

| Column | Meaning |
|--------|---------|
| `imb` | Top-of-book imbalance in [-1, 1]: `(bid_qty - ask_qty) / (bid_qty + ask_qty)`. +1 means all size is on the bid. |
| `OFI` | Order-flow imbalance, **cumulative since the current connection started** (see below). |
| `upd/s` | Order book deltas received in the last frame, scaled to per-second. Because frames are 0.1s apart, values move in steps of 10. |

A market shows `waiting for two-sided book` until it has both a bid and an ask.

## How it works

```
main()
 ├─ get_series_list / get_live_markets      REST: which markets exist?
 └─ orderbook_websocket(tickers)            connect / reconnect loop
      ├─ writer_task  ◄── asyncio.Queue ◄───────────┐
      └─ handle_messages  (one per connection)      │
           ├─ subscribe_to_ws                       │
           ├─ receive loop ── enqueue raw message ──┘
           │      └─ demux on market_ticker
           │            └─ classify_message → that market's MarketState
           └─ display_loop (painter task)
```

1. **Discovery** (`connection_utils.py`): REST calls list series and open
   markets. These are public endpoints and are not signed.
2. **Connection** (`orderbook_websocket`): builds freshly signed headers for
   each attempt, connects with ping/pong keepalive, and retries after a
   `ConnectionClosed` or `OSError` with a fixed 3 second delay.
3. **Subscription** (`subscribe_to_ws`): one `subscribe` command for the
   `orderbook_delta` and `trade` channels covering all tickers. The server
   sends one snapshot per market, then deltas.
4. **Receive loop** (`handle_messages`): for every frame, stamp the receive
   time and put the raw text on a queue (nothing else touches the disk here),
   then parse it and route it by `market_ticker` to that market's state.
5. **Logging** (`writer_task`): a single task drains the queue and appends to
   an hourly file, choosing the file from each message's timestamp. It flushes
   when the queue is empty.
6. **Display** (`display_loop`): an independent timer task that reads every
   `MarketState` and repaints the block. It is decoupled from the message rate.

Background tasks (`writer_task`, `display_loop`) share objects with the
receive loop by reference; nothing is copied. Because asyncio tasks only switch
at `await` points and none of the book updates contain one, no locks are needed.

### Message types handled (`classify_message`)

| Type | Action |
|------|--------|
| `subscribed` | Logged. |
| `orderbook_snapshot` | Replaces the book and marks it `ready`. |
| `orderbook_delta` | Applies a quantity change at one price level, updates OFI, counts the update. Ignored until the snapshot has arrived. |
| `trade` | Counts the trade and records the last price. |
| `error` / anything else | Logged as a warning. |

### Order book model (`data_models.py`)

Kalshi sends two sets of **bids**:

- `yes`: resting orders to buy Yes at price `p`, which is a Yes bid.
- `no`: resting orders to buy No at price `q`, which is equivalent to selling
  Yes at `1 - q`, a Yes ask.

So in Yes terms: `best_bid = max(yes prices)` and `best_ask = 1 - max(no prices)`.

- `OrderBook` stores `price_string -> quantity` for each side, rebuilt from a
  snapshot and updated by deltas (a delta is a *change* in size at a level;
  levels at or below zero are removed).
- `OFITracker` implements order-flow imbalance (Cont, Kukanov, Stoikov). On
  each update it compares the best bid/ask price and size against the previous
  state and accumulates the signed contribution. `reset()` returns and zeroes
  the running total, but the live script does not call it yet.
- `MarketState` bundles a market's `ticker`, `book`, `ofi`, and `stats`
  counters (`deltas`, `trades`, `last_trade`).
- `Trade` is a frozen dataclass for trade records (not yet used by the live
  path).

## Data on disk

Raw messages go to:

```
data/YYYYMMDD/orderbook_HH.txt        (America/New_York date and hour)
```

Each line is:

```json
{"timestamp": 1791412345123456789, "msg": "<the raw websocket frame, as a JSON string>"}
```

- `timestamp` is `time.time_ns()` taken when the frame was received.
- `msg` is the original frame **as a string**, so reading a line takes two
  parses: `json.loads(line)`, then `json.loads(record["msg"])`.
- All markets share the same hourly file; the ticker is inside each frame.

The app's own log (connections, snapshots, warnings) goes to
`logs/kalshi_example_YYYYMMDD.log`.

`data/`, `*.txt`, `*.json`, `*.zip`, and `.env` are git-ignored.

## Files

| File | Purpose |
|------|---------|
| `kalshi_example.py` | Entry point: CLI, market discovery, WebSocket loop, logging queue, display. |
| `connection_utils.py` | RSA-PSS request signing, header creation, REST helpers (`get_live_markets`, `get_series_list`) with 429 retry. |
| `data_models.py` | `OrderBook`, `OFITracker`, `MarketState`, `Trade`, and the `NonEmptyBook` / `InvalidMarket` exceptions. |
| `data_verifiers.py` | `check_file` (every line and inner message parses), `replay_json` (rebuild a book from a log), `clean_data` (stub). |
| `copy_files.sh` | Nightly cron job (23:55): zip the previous day's `data/YYYYMMDD`, copy it to the external drive, email on error. Work in progress. |
| `driver.sh` | Placeholder. |

## Known limitations

- **No sequence checking.** Deltas carry a sequence number; if one is missed
  the local book silently drifts and nothing detects it.
- **Reconnect is basic.** Fixed 3 second delay, no backoff, no markers in the
  data marking where the gaps are, and authentication errors are not handled.
- **Only some errors are caught.** One malformed frame or unexpected field can
  end the process, and the writer and painter tasks can die without notice.
- **Market list is fixed at startup.** New games and settled markets are not
  picked up until restart. `get_live_markets` also truncates to 100 markets per
  series by default.
- **OFI is cumulative** and resets on reconnect; it is not yet windowed.
- **`replay_json` assumes one market.** It applies every message to a single
  book, so it is only correct for single-ticker logs.
- **The display needs a terminal** at least as tall as the number of markets.
- **Not yet schedulable.** Relative paths, no shutdown handling, no
  single-instance lock.