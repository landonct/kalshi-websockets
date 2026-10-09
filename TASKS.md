# Task List: Kalshi Order Book Collector

Every task has a **Verify** step. A task is done when its verify step passes,
not when the code looks right.

**Priorities**
- `P0`: can lose data, crash, or leak a secret. Do these first.
- `P1`: correctness or reliability problem you will hit eventually.
- `P2`: cleanup, performance, polish.

**⚠** marks a task about a file I have not seen recently (`data_models.py`,
`data_verifiers.py`, `copy_files.sh`). If it's already fixed, run its verify
step anyway, then check the box.

**Suggested order:** Phase 0 → 1 → 4.1–4.3 → 3 → 2 → 4 (rest) → 5 → 6 → 7 → 8 → 9 → final soak test.

---

## Phase 0: Test harness (do this first, so everything else is checkable)

- [x] **0.1 Set up pytest** `P0`
  - Add `tests/`, install `pytest` and `pytest-asyncio`, add a `pyproject.toml` (or `pytest.ini`).
  - Verify: `pytest -q` runs and reports "no tests ran" without errors.

- [x] **0.2 Capture a fixture from real data** `P0`
  - Copy the first ~2,000 lines of a real hour file (it should contain the `subscribed` line, snapshots, deltas, and some trades) to `tests/fixtures/sample.jsonl`.
  - Use the `.jsonl` extension, because your `.gitignore` ignores `*.txt` and `*.json`.
  - Verify: `python -c "import json; [json.loads(json.loads(l)['msg']) for l in open('tests/fixtures/sample.jsonl')]"` exits cleanly.

- [x] **0.3 Make the WebSocket URL overridable** `P0`
  - `WS_URL = os.getenv("KALSHI_WS_URL", "wss://external-api-ws.kalshi.com/trade-api/ws/v2")`.
  - Verify: setting `KALSHI_WS_URL=ws://localhost:8765` makes the script try to connect locally.

- [ ] **0.4 Build a fake server that replays your fixture and injects faults** `P0`
  - This is how you test reconnects, garbage frames, and shutdown without waiting for the real network to misbehave.
  - Frames are replayed verbatim from the `msg` field, so the shapes are the real ones. Run it with `python tests/fake_server.py drop`.
  - You also need a throwaway key for the signing code: `openssl genrsa -out /tmp/test.pem 2048`, with `PRIVATE_KEY_PATH=/tmp/test.pem` and any value for `KALSHI_ACCESS_KEY`. The fake server ignores the headers.

```python
# tests/fake_server.py
import asyncio, json, sys
from websockets.asyncio.server import serve

FIXTURE = "tests/fixtures/sample.jsonl"
MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
# modes: normal | drop | garbage | error | silent | seqgap | badfield

async def handler(ws):
    await ws.recv()                                    # the subscribe command
    frames = [json.loads(l)["msg"] for l in open(FIXTURE)]
    for i, frame in enumerate(frames):
        if MODE == "drop" and i == 500:
            await ws.close(code=1011); return          # server-side failure
        if MODE == "garbage" and i % 100 == 50:
            await ws.send("{not json")
        if MODE == "silent" and i == 500:
            await asyncio.sleep(3600)                  # connected but no data
        await ws.send(frame)
        await asyncio.sleep(0.001)

async def main():
    async with serve(handler, "localhost", 8765):
        await asyncio.Future()

asyncio.run(main())
```

  - Extend `error`, `seqgap`, and `badfield` yourself as you reach the tasks that use them: send a `{"type":"error",...}` frame, skip or duplicate a `seq`, and drop the `side` field from one delta.
  - Verify: run the fake server in `normal` mode, point the collector at it, and see one painter line per market in the fixture.

- [ ] **0.5 Add linters and run them** `P2`
  - `ruff check .`, `mypy kalshi_example.py data_models.py connection_utils.py data_verifiers.py`, `shellcheck copy_files.sh`.
  - Verify: all three report clean (or only suppressions you've reviewed).

---

## Phase 1: Crash safety

- [ ] **1.1 Per-message exception handling in the receive loop** `P0`
  - Wrap parse + classify in `try/except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError)`. Log the raw frame, increment an `errors` counter, `continue`.
  - Today only `ConnectionClosed` and `OSError` are caught anywhere, so one malformed frame ends the process.
  - Verify: run the fake server in `garbage` mode. The process keeps running, the `errors` counter equals the number of frames injected, and each bad frame appears in the app log. Also run `badfield` mode (a delta with no `side`) and confirm the same.

- [ ] **1.2 Supervise background tasks (painter and writer)** `P0`
  - Add `task.add_done_callback(...)` that logs any exception. A dead **writer** is fatal: stop the program with a non-zero exit, because the queue would otherwise grow forever while you believe you're collecting.
  - Verify (writer): make `data/` read-only (`chmod -w data`) before the first write. Expect a logged error and exit code ≠ 0 within a few seconds, not a silent hang.
  - Verify (painter): in a test, monkeypatch `book_imbalance` to raise. The exception must show up in the log.

- [ ] **1.3 Make the painter exception-safe** `P0`
  - Wrap each market's line in `try/except`, and render a placeholder line like `ticker | display error` for the failing market.
  - Verify: the painter test from 1.2 passes, and the other markets' lines still render.

- [ ] **1.4 Handle server `error` and the subscribe acknowledgement** `P0`
  - Check `msg_type == "error"` **before** the "no market_ticker → control message" `continue`, and log it at WARNING. After subscribing, require a `subscribed` ack within N seconds, otherwise treat the connection as failed and reconnect.
  - Verify: fake server `error` mode produces a WARNING in the log. A fake server that never acks triggers a reconnect after the timeout.

- [ ] **1.5 Handle handshake failures** `P0`
  - `websockets.exceptions.InvalidStatus` (bad credentials, clock skew, rate limit) is not an `OSError` and currently crashes. Decide per status code: 401/403 log clearly and exit non-zero; 429/5xx back off and retry. Check what Kalshi returns, and write it in a comment.
  - Verify: have the fake server reject the handshake with 401 (e.g. via `process_request`). The collector logs the cause and exits non-zero without looping. With a 429, it retries with growing delays.

- [ ] **1.6 Startup validation and absolute paths** `P0`
  - Fail with a clear one-line message (no traceback) if `KALSHI_ACCESS_KEY` or `PRIVATE_KEY_PATH` is unset or the key file is missing.
  - Anchor `.env`, `logs/` and `data/` to `Path(__file__).resolve().parent`, so cron and systemd don't depend on the working directory.
  - Verify: `env -u KALSHI_ACCESS_KEY python kalshi_example.py --series X` prints one clean error and `echo $?` is non-zero. Running from another directory (`cd /tmp && python /home/lando/kalshi-websockets/kalshi_example.py ...`) still writes into the repo's `data/` and `logs/`.

- [ ] **1.7 Retries on startup REST calls** `P1`
  - `get_live_markets` and `get_series_list` currently raise on the first network error. Wrap discovery in a retry with backoff (see 6.1).
  - Verify: run with the network off, then turn it on within the retry window. Startup succeeds.

- [ ] **1.8 Meaningful exit codes** `P1`
  - Clean shutdown (SIGINT/SIGTERM) exits 0. Fatal errors exit non-zero. Systemd and cron decide whether to restart based on this.
  - Verify: `echo $?` after each case: Ctrl-C → 0, fatal → non-zero.

- [ ] **1.9 Don't record sentinel prices** `P2`
  - `float(msg.get("yes_price_dollars", -1.0))` stores `-1.0` as a trade price when the field is missing. Skip the update and count it as a malformed trade instead.
  - Verify: unit test passing a trade without that field leaves `last_trade` unchanged and increments an error counter.

---

## Phase 2: Order book correctness (`data_models.py`) ⚠

- [ ] **2.1 Remove float drift in quantities** `P0`
  - `book.get(price) + delta` in floats leaves residue like `5.5e-17`, so levels that should be empty are kept, and the `v >= 1` filter then hides them. Store integer hundredths (or `Decimal`).
  - Verify (this test fails today):
```python
def test_float_drift_removes_level():
    book = OrderBook("T")
    book.apply_snapshot({"yes_dollars_fp": [], "no_dollars_fp": []})
    for d in ("0.1", "0.2", "-0.3"):
        book.apply_delta({"side": "yes", "price_dollars": "0.55", "delta_fp": d})
    assert "0.55" not in book.yes
```

- [ ] **2.2 Normalise price keys** `P0`
  - `"0.55"` and `"0.5500"` currently become two levels. Convert to integer cents/hundredths or `Decimal` before using as a key.
  - Verify: a snapshot with `"0.55"` followed by a delta at `"0.5500"` changes the **same** level.

- [ ] **2.3 Pick one schema, fail loudly on the other** `P1`
  - `msg.get("price_dollars", msg.get("price"))` falls back to a legacy field that is in cents. A key of `"55"` would sit in a dollars book and win `max()` as a 55.00 bid.
  - Verify: a delta with `{"price": 55}` raises/logs and does **not** add a level.

- [ ] **2.4 Validate `side`** `P1`
  - `self.yes if side == "yes" else self.no` sends any other value (typo, new schema) into the No book silently. Accept only `"yes"` and `"no"`.
  - Verify: `apply_delta({"side": "maybe", ...})` raises.

- [ ] **2.5 Fix `best_yes_bid` / `best_yes_ask` robustness** `P0`
  - After 2.1, remove the `v >= 1` filter (decide whether fractional quantities from the `_fp` fields are meaningful, and document it). Both functions must return `None` on an empty side instead of raising.
  - Verify: a property test that applies random delta sequences and compares best bid/ask to a brute-force reference using `Decimal`. It must never raise and must always agree.

- [ ] **2.6 Crossed-book invariant check** `P1`
  - In a healthy book the best Yes bid is strictly below the best Yes ask (bid + best No bid < $1.00). If it is not, the book is corrupt or mid-update. Count occurrences and log a WARNING if the condition persists across several updates.
  - Verify: replay a full day (5.2) and report the crossed count. Expect ~0. Investigate any sustained run.

- [ ] **2.7 `OFITracker`: don't difference across gaps** `P1`
  - `update()` returns early when a side is empty but keeps the old `_prev`, so the next event is compared to a state from before the gap. Set `self._prev = None` there.
  - Verify: a test where the ask disappears for a few updates and then returns. The OFI must not jump by the cross-gap difference.

- [ ] **2.8 Hand-checked OFI test** `P1`
  - The six branches (bid up / equal / down, ask down / equal / up). Start from bid `(0.50, 10)` and ask `(0.52, 8)`; hold the other side fixed:

```python
class FakeBook:
    def __init__(self, bid, ask): self._b, self._a = bid, ask
    def best_yes_bid(self): return self._b
    def best_yes_ask(self): return self._a

@pytest.mark.parametrize("bid, ask, expected", [
    ((0.51, 5),  (0.52, 8),  +5),   # bid price up   -> +new bid qty
    ((0.50, 7),  (0.52, 8),  -3),   # bid same px    -> change in bid qty
    ((0.49, 12), (0.52, 8), -10),   # bid price down -> -old bid qty
    ((0.50, 10), (0.51, 4),  -4),   # ask price down -> -new ask qty
    ((0.50, 10), (0.52, 5),  +3),   # ask same px    -> -(change in ask qty)
    ((0.50, 10), (0.53, 6),  +8),   # ask price up   -> +old ask qty
])
def test_ofi_branches(bid, ask, expected):
    t = OFITracker()
    t.update(FakeBook((0.50, 10), (0.52, 8)))
    t.update(FakeBook(bid, ask))
    assert t.ofi == expected
```

- [ ] **2.9 Window the OFI** `P2`
  - The live script never calls `reset()`, so the displayed OFI is cumulative since the connection started (the numbers in your output reach six figures).
  - Verify: after a reset, the displayed OFI restarts near zero, and the sum of windowed values equals the cumulative value.

- [ ] **2.10 Performance of best bid/ask** `P2`
  - Both rebuild and re-parse the whole dict on every call (O(n)), including every repaint. Cache the best price, or keep keys as integers and use a heap/sorted structure.
  - Verify: benchmark 100k deltas on a 10k-level book before and after, and record both numbers. Correctness is guarded by 2.5.

- [ ] **2.11 Decide the `Trade` type, or delete it** `P2`
  - `Trade.price: int` doesn't match the dollar-string prices in live trade messages, and the class is unused. Settle on cents vs dollars and either use it or remove it.
  - Verify: `grep -rn "Trade(" .` matches only intended uses.

---

## Phase 3: Reconnect and shutdown

- [ ] **3.1 Exponential backoff with jitter** `P0`
  - 1s, 2s, 4s, … capped at 60s, with random jitter. Reset the delay only after a connection has stayed healthy for ~60s.
  - Verify: fake server in `drop` mode (disconnect every connection). The log shows increasing delays. With a connection that stays up for >60s, the delay resets.

- [ ] **3.2 Log the connection lifecycle** `P0`
  - On connect, log tickers count and attempt number. On disconnect, log the reason, duration, and messages received. A clean server-side close ends `async for` **without** an exception, and is currently silent.
  - Verify: each fake-server drop produces exactly one connect line and one disconnect line.

- [ ] **3.3 Write connect/disconnect markers into the data files** `P0`
  - Enqueue `{"timestamp": ..., "event": "connect"|"disconnect", "reason": ...}` through the same queue. Books and OFI restart after each reconnect, so research code needs to know where the gaps are.
  - These lines have no `msg` key, so `replay_json` and `check_file` must accept them (5.2, 5.3).
  - Verify: after a run with N forced drops, the data file has exactly N+1 connect markers and N disconnect markers, in order.

- [ ] **3.4 Sequence-number gap detection** `P0`
  - Track the last `seq` per subscription (`sid`). On a gap, log it, write a marker, and force a reconnect to get fresh snapshots. First confirm the real field names and where they live in a raw line of your log.
  - Verify: fake server `seqgap` mode (skip one `seq`). The collector detects it, writes the marker, and reconnects. With no gaps (normal mode), no false positives across the whole fixture.

- [ ] **3.5 No leaked tasks across reconnects** `P1`
  - Each connection creates a painter. Confirm the old one is cancelled and awaited.
  - Verify: log `len(asyncio.all_tasks())` on every connect. After 10 forced reconnects, the number is the same as after the first.

- [ ] **3.6 Fresh state on every connection** `P1`
  - New `MarketState` objects per connection are correct. Confirm no stale book survives, and the painter shows "waiting" until new snapshots arrive.
  - Verify: after a drop, every market line shows the waiting state, then recovers. The number of snapshots per market equals the number of connections.

- [ ] **3.7 Graceful shutdown on SIGINT and SIGTERM** `P0`
  - `loop.add_signal_handler(signal.SIGTERM, stop.set)` (and SIGINT). On stop: close the websocket, drain the queue, flush, close the file, then exit 0. `KeyboardInterrupt` currently cancels the writer with messages still queued, and SIGTERM runs no cleanup at all.
  - Verify: run for a minute, `kill -TERM <pid>`, then `tail -c 300` on the data file. It ends with a complete JSON line, and the counters in 4.1 show received == written.

- [ ] **3.8 Silence watchdog** `P2`
  - Ping/pong detects dead transport, but not "connected and silent". Reconnect if **no frame at all** (including control messages) arrives for N minutes. Choose N generously, since quiet overnight markets are normal.
  - Verify: fake server `silent` mode triggers a reconnect after N.

- [ ] **3.9 Subscription limits** `P0`
  - Look up Kalshi's limits on markets per subscription and connections per account. If a subscription can't carry all your markets, split into several `subscribe` commands or several connections.
  - Verify: the number of markets in the `subscribed` acknowledgements equals the number requested. Log both.

- [ ] **3.10 Partial subscription failures** `P1`
  - Some markets might be rejected (closed, wrong ticker). Log which, and continue with the rest.
  - Verify: include one invalid ticker in a real run. The others still stream, and the bad one is logged.

---

## Phase 4: Logging correctness

- [ ] **4.1 Counters and the "received == written" invariant** `P0`
  - Count frames received, enqueued, written, and errors. Log them plus `queue.qsize()` every 60s, and once at shutdown.
  - Verify: after a clean shutdown, `received == written`. Cross-check against the file: `wc -l` on the day's files equals `written` (plus marker lines).

- [ ] **4.2 Flush on a timer and handle write failures** `P0`
  - Today it flushes only when the queue is empty, which never happens under sustained load, so a hard kill can lose a lot. Flush about once a second. Catch `OSError` from `open`/`write` (disk full, permissions), log it, and treat it as fatal.
  - Verify: `kill -9` the process mid-run. You lose at most ~1s of data, and every complete line parses. Fill a small tmpfs to test disk-full: expect a logged error and non-zero exit.

- [ ] **4.3 Queue depth monitoring** `P1`
  - Warn when `qsize()` stays above a threshold.
  - Verify: slow the writer artificially (`await asyncio.sleep(0.01)` per item). The warning fires. Remove the delay and it stops.

- [ ] **4.4 Decide the line format once** `P1`
  - Today `msg` is a JSON string inside JSON (double-encoded), which inflates files and needs two parses. If you change it, change the writer, `replay_json`, `check_file`, the ETL, and the README together.
  - Verify: measure bytes per hour of the current format against the alternative on the same fixture before deciding. Record the numbers in the README.

- [ ] **4.5 Consistent timezone for filenames and cron** `P1`
  - The logger names directories by America/New_York time, while `copy_files.sh` uses `date -d yesterday` in the system timezone. In New York time the DST fall-back hour also maps two different hours to one filename. Pick one zone (UTC avoids both problems) and use it in both places.
  - Verify: `TZ=America/New_York date +%Y%m%d` vs `date +%Y%m%d` vs the directory name written by the collector at 23:30 and 00:30 local time. Test `log_path` with synthetic timestamps around midnight and around 2026-11-01 01:30.

- [ ] **4.6 Hour/day rotation tested** `P1`
  - Feed `writer_task` messages with timestamps across an hour and day boundary.
  - Verify: files appear as `data/<date>/orderbook_<HH>.txt`, the earlier file is closed, and no line lands in the wrong file.

- [ ] **4.7 Resolve the `--log` flag** `P1`
  - It only logs one line, while data is always written. Either gate the writer on it or delete the flag and update the README.
  - Verify: run with and without it and confirm the behaviour matches the `--help` text.

- [ ] **4.8 App log rotation and naming** `P1`
  - The log filename is computed at import, so a process running past midnight keeps writing yesterday's file. Use `TimedRotatingFileHandler`, a named logger (`getLogger(__name__)`), and set the `websockets` logger level explicitly.
  - Verify: run with a short rotation interval (e.g. 1 minute) in a test and see multiple files.

- [ ] **4.9 Disk space estimate and startup check** `P1`
  - Measure bytes per hour from real data, multiply by 24, and compare against free space on the local disk and the external drive. Refuse to start (or warn) if less than N days remain.
  - Verify: print the estimate at startup; check it against `du` after a real day.

- [ ] **4.10 Clock sync** `P1`
  - Receive timestamps use the wall clock, and request signing depends on it too.
  - Verify: `timedatectl` shows `System clock synchronized: yes`.

- [ ] **4.11 Burst test** `P2`
  - Verify: enqueue 1M synthetic messages. The writer drains them in a sensible time and memory returns to baseline.

- [ ] **4.12 `fsync` on file close/rotation** `P2`
  - Verify: not needed unless you care about power loss. Decide, and note it.

---

## Phase 5: Replay and verification tools (`data_verifiers.py`) ⚠

- [ ] **5.1 No work at import time** `P0`
  - Earlier versions ran `replay_json` at module level and imported a constant from `kalshi_example`. Anything like that creates import cycles and surprise I/O.
  - Verify: `python -c "import data_verifiers"` prints nothing, opens no files, and exits instantly.

- [ ] **5.2 Make `replay_json` multi-market** `P0`
  - It applies every message to one `OrderBook`, which corrupts books for multi-market logs. Route by `market_ticker` into `dict[str, OrderBook]`. Skip `event` marker lines, and stop printing every `trade` as "unhandled".
  - Verify: replay a multi-market fixture. The number of books equals the number of markets, and each book's final best bid/ask equals what a single-ticker replay of only that market's messages gives.

- [ ] **5.3 Turn `check_file` into a real report** `P1`
  - Return a dict instead of printing. Checks: every line parses, the inner message parses, timestamps are non-decreasing, `event` markers are well-formed, one snapshot per market per connection, `seq` is continuous within a connection, crossed-book count.
  - Verify: tests with a good fixture (all pass) and a deliberately broken copy (truncated line, swapped timestamps, missing snapshot) that each get flagged.

- [ ] **5.4 Live-vs-replay equivalence test** `P0`
  - This is the single best test of the whole pipeline. At shutdown, write each market's final best bid/ask and book size to a small summary file. Replay the day's data file and compare.
  - Verify: for a fake-server run (deterministic), live final state == replayed final state for every market.

- [ ] **5.5 Implement or delete `clean_data`** `P2`
  - It's a stub that parses and discards.
  - Verify: `grep -rn clean_data .` shows a real use, or it's gone.

---

## Phase 6: Market discovery (`connection_utils.py`)

- [ ] **6.1 Fix the retry URL in `get_series_list`** `P0`
  - The retry loop calls `requests.get(URL, ...)` (the markets endpoint) instead of `SERIES_URL`, and the first request has no timeout.
  - Verify: unit test with `requests.get` monkeypatched to return 429 then 200. Assert the second call's URL is `SERIES_URL`.

- [ ] **6.2 Always return the same type** `P0`
  - It returns a DataFrame when `include_volume` is true and a Series otherwise, so `series["tickers"]` raises `KeyError` for `--category` without `--volume`.
  - Verify: both call styles work end to end with `--category <name>`, with and without `--volume`.

- [ ] **6.3 Check how booleans are sent** `P1`
  - `params={"include_volume": True}` is encoded by `requests` as `"True"`. Check what the API expects (probably lowercase `true`), and use `str(x).lower()` if needed.
  - Verify: print the prepared request URL, and confirm volumes actually come back populated.

- [ ] **6.4 Make `get_live_markets` limits explicit** `P1`
  - The default `limit=100` silently truncates a series. Use `limit=None` for "all", and log when truncation happens.
  - Verify: for a series with more than 100 open markets, the count returned matches the exchange's own count.

- [ ] **6.5 Sort order** `P2`
  - `sort_values("volume")` is ascending, so the biggest series are last. Probably you want descending.
  - Verify: the first row has the maximum volume.

- [ ] **6.6 Deduplicate tickers** `P1`
  - Verify: `len(all_tickers) == len(set(all_tickers))` (use `dict.fromkeys` to dedupe while preserving order).

- [ ] **6.7 Make the series filter configurable** `P2`
  - `GAME|MATCH` is hard-coded in `main()`. Add a `--series-filter` regex flag with that default.
  - Verify: `--help` shows it, and a different regex changes the list.

- [ ] **6.8 Reuse a `requests.Session` and set timeouts everywhere** `P2`
  - Verify: no `requests.get(` call without `timeout=`: `grep -n "requests.get" connection_utils.py`.

- [ ] **6.9 Remove dead code in `connection_utils.py`** `P2`
  - `except InvalidSignature` never triggers (`sign()` doesn't raise it), `default_backend()` is unneeded, and the `ZoneInfo` in `create_headers` does nothing (epoch time has no timezone). Use `time.time()`.
  - Verify: signing still works against the real API (a run that connects and gets snapshots).

---

## Phase 7: Entry point (`kalshi_example.py`)

- [ ] **7.1 Use argparse for mutual exclusion** `P1`
  - `--series`, `--category`, and `--file` are mutually exclusive, and one is required. Use `add_mutually_exclusive_group(required=True)` and drop the manual `print(usage)` + `raise InvalidMarket`.
  - Verify: passing none or two of them exits with an argparse usage error.

- [ ] **7.2 Painter fits the terminal** `P0`
  - The cursor-up trick breaks once the block is taller than the screen. Use `shutil.get_terminal_size()`, show at most (rows − 2) markets sorted by update rate, and add a summary line ("showing 20 of 240 markets").
  - Verify: run with 100 markets in a 24-row terminal. No scrolling or garbage, and the summary line is right.

- [ ] **7.3 `upd/s` over a real window** `P2`
  - Frames are 0.1s apart, so the rate moves in steps of 10. Compute it over a 1-second window.
  - Verify: a steady 5 updates/s feed shows ~5, not alternating 0 and 10.

- [ ] **7.4 Replace the `stats` dict with a dataclass** `P2`
  - `dict[str, float | None]` with `is not None` checks everywhere is noisy and type-unsound. Use a dataclass with integer counters.
  - Verify: `mypy` is clean, and the `None` branches in `classify_message` are gone.

- [ ] **7.5 Remove dead code and update docstrings** `P2`
  - `log_message`, the `TextIO` import, the unused `method` variable, and the `display_loop` docstring (it still says "a single status line" and `\r`).
  - Verify: `ruff check` reports no unused names.

- [ ] **7.6 Dedicated test for `classify_message`** `P1`
  - Verify: tests feed snapshot → delta → trade → error from the fixture and assert the resulting book, `deltas`, `trades`, and `last_trade`.

---

## Phase 8: Making it schedulable

- [ ] **8.1 Secrets hygiene** `P0`
  - Your `.gitignore` covers `.env` and `secret`, but **not** `*.pem`. If the key lives inside the repo it could be committed.
  - Add `*.pem` and `*.key`, set `chmod 600` on the key and `.env`.
  - Verify: `git check-ignore -v your_key.pem` prints a rule. `git log --all --oneline -- '*.pem'` is empty. `git grep -n "BEGIN .*PRIVATE KEY" $(git rev-list --all)` finds nothing.

- [ ] **8.2 Single-instance lock** `P0`
  - Use `flock` on a lock file (or a pidfile). Two collectors writing the same files would interleave lines.
  - Verify: start a second instance. It exits immediately with a clear message, and the first keeps running.

- [ ] **8.3 No ANSI output when not on a terminal** `P0`
  - Skip the painter when `not sys.stdout.isatty()`.
  - Verify: `python kalshi_example.py ... | cat | grep -c $'\033'` prints `0`.

- [ ] **8.4 Refresh the market list** `P1`
  - New games appear daily, and finished markets settle, but the list is fixed at startup. Simple option: a nightly restart. Better: re-query periodically and add/remove markets on the live subscription. Run `requests` calls through `asyncio.to_thread` so they don't block the loop.
  - Verify: while running, a market that gets added to the exchange (or a settled market) shows up in (or drops out of) the painter within one refresh interval, with a marker in the data file.

- [ ] **8.5 Reproducible install** `P1`
  - Add `requirements.txt` or `pyproject.toml`. Your `.gitignore` has `*.txt`, so whitelist it with `!requirements.txt`.
  - Verify: in a fresh venv, `pip install -r requirements.txt && python kalshi_example.py --help` works.

- [ ] **8.6 Systemd service for the collector** `P1`
  - A long-running process fits systemd better than cron. Example:

```ini
# ~/.config/systemd/user/kalshi-collector.service
[Unit]
Description=Kalshi order book collector

[Service]
WorkingDirectory=/home/lando/kalshi-websockets
ExecStart=/home/lando/kalshi-websockets/.venv/bin/python kalshi_example.py --category <name>
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
```

  - Verify: `systemctl --user start kalshi-collector`, then `kill -9` the main process. It restarts within ~10s. `systemctl --user stop` shuts down cleanly (3.7), and `journalctl --user -u kalshi-collector` has no escape codes. For restarts at boot without a login, check `loginctl enable-linger`.

- [ ] **8.7 Health check and alerts** `P1`
  - Touch a heartbeat file every minute, and have a cron job email you if it is older than N minutes. Also email on fatal exit.
  - Verify: stop the collector. An alert arrives within the window.

- [ ] **8.8 Local retention policy** `P2`
  - Decide how long local copies are kept after a successful copy to the external drive (the "sleep timer" idea in your script).
  - Verify: a test directory older than N days is removed only if the copy's checksum matched.

---

## Phase 9: Nightly copy job (`copy_files.sh`) ⚠

- [ ] **9.1 Remove any leftover debug `die`** `P0`
  - A version I saw had `die "i'm stopping!"` before the `zip` line, which aborts and emails you every night.
  - Verify: `grep -n "stopping" copy_files.sh` prints nothing.

- [ ] **9.2 Fix the `rsync` source** `P0`
  - `"${ZIP_FILE}/"` has a trailing slash on a file, so rsync treats it as a directory and fails. Use `"$ZIP_FILE"`.
  - Verify: run against a test directory and a temporary destination. The zip arrives.

- [ ] **9.3 Copy, verify, then delete** `P0`
  - `--remove-source-files` deletes the zip before your planned checksum. Run `rsync` without it, compare `sha256sum` of source and destination, then remove the local zip only if they match. Log both checksums.
  - Verify: corrupt the destination copy (append a byte) and rerun. The script must fail and keep the local files.

- [ ] **9.4 Check that the external drive is mounted** `P0`
  - If `/d` isn't mounted, `mkdir -p /d/...` creates a directory on the root filesystem and fills the local disk. Use `mountpoint -q /d || die "external drive not mounted"` before anything else.
  - Verify: unmount the drive (or point `DATA_DEST` at a non-mountpoint). The script exits non-zero and creates nothing under `/`.

- [ ] **9.5 Space check and fallback (your TODO #4)** `P1`
  - Compare `df --output=avail -B1 /d` against `du -sb "$DATA_DIR"`. If it won't fit, write the zip to a fallback directory locally and email you.
  - Verify: use a 1 MB tmpfs as the destination (`sudo mount -t tmpfs -o size=1m tmpfs /tmp/small`) with a larger source. The fallback path is taken and the email arrives.

- [ ] **9.6 Fix `on_err`** `P1`
  - `log "$err_msg" >&2` passes the message as the *level* argument, and `mail -s "..."` has no recipient (unlike `die`). Use `log ERROR "$err_msg"` and pass the address.
  - Verify: force an error (e.g. a missing source directory). The log line is formatted correctly and the email arrives.

- [ ] **9.7 `die` mails once, and everything uses the same recipient** `P2`
  - `die` mails, then `exit 1` triggers `trap ERR`, which may mail again. Move the address into a variable (or `.env`) rather than hard-coding.
  - Verify: one forced failure produces one email.

- [ ] **9.8 `flock` and `zip -T`** `P1`
  - Prevent overlapping runs, and test the archive after creating it.
  - Verify: run the script twice at once. The second exits. Corrupt a zip and confirm `zip -T` fails the run.

- [ ] **9.9 Compression value** `P2`
  - Text logs compress well, but if you move to a binary format (Parquet/DuckDB), zip gains little.
  - Verify: compare `du -sb` of the directory with the zip. If the ratio is near 1, just `rsync` the directory.

- [ ] **9.10 Works under cron's environment** `P0`
  - Cron has a minimal `PATH`, no shell profile, and a different working directory. `mail` also needs a working MTA.
  - Verify: `env -i /bin/bash /full/path/copy_files.sh` succeeds. Send a test `mail` from the cron user and confirm delivery.

- [ ] **9.11 Date logic matches the collector** `P1`
  - See 4.5. The 23:55 job copies "yesterday", so confirm the directory name it computes matches the one the collector wrote.
  - Verify: print `DATA_DIR` at the top of the run and check that it exists.

- [ ] **9.12 Dry-run flag and `shellcheck`** `P2`
  - Verify: `./copy_files.sh --dry-run` logs what it would do and changes nothing. `shellcheck copy_files.sh` is clean.

- [ ] **9.13 Fill in or delete `driver.sh` and the empty TODO #5** `P2`
  - Verify: no placeholder files or empty TODOs remain: `grep -rn "TODO" *.sh *.py`.

---

## Phase 10: Documentation

- [ ] **10.1 Keep the README honest** `P1`
  - After each phase, update the "Known limitations" section and anything in "How it works" that changed (markers, formats, flags, paths).
  - Verify: every item removed from "Known limitations" has a passing test or verify step above.

- [ ] **10.2 Document operations** `P2`
  - Add a short runbook: how to start/stop, where logs and data live, what each alert means, how to replay a day, how to recover from a gap.
  - Verify: someone else (or you in three months) can follow it from a clean checkout.

---

## Final acceptance: 24-hour soak test

Run the collector under systemd on a real category for 24 hours, with one deliberate network outage (60s) during the run. Then:

- [ ] Received == written in the shutdown counters (4.1).
- [ ] `check_file` reports 0 exceptions on all 24 hour-files (5.3).
- [ ] Connect/disconnect markers match the number of reconnects in the app log (3.3).
- [ ] One snapshot per market per connection (3.6).
- [ ] Crossed-book count is ~0 (2.6).
- [ ] Live-vs-replay summaries match for every market (5.4).
- [ ] Memory (RSS from `ps` sampled hourly) is flat, and queue depth stays bounded.
- [ ] The nightly copy job runs by itself, the zip checksum matches, and local cleanup follows policy (Phase 9).
- [ ] An alert fires when you stop the collector on purpose (8.7).