# Kalshi Collector: Library Internals, C++ Port, and Operations Guide

*Prepared for the kalshi-websockets project, October 19, 2026. Covers `kalshi_example.py`, `connection_utils.py`, `data_models.py`, `data_verifiers.py`, `copy_files.sh`, and `tests/fake_server.py`.*

## 0. How to use this document

The first half explains every third-party library call in your code: what it does at the protocol level, what is happening underneath, how it maps to C++, and how to get the most out of it. The second half covers the project-level material you asked for: the C++ port architecture, compression, data management, shell scripting, and systemd.

The third-party dependencies in the repo are small. Everything else is the standard library.

| Library | Where it is used | What it does for you | C++ counterpart |
| --- | --- | --- | --- |
| `cryptography` | `connection_utils.py` | Loads your RSA key, signs the request with RSA-PSS/SHA-256 | OpenSSL (`EVP_*` API) |
| `websockets` | `kalshi_example.py`, `tests/fake_server.py` | WebSocket client and server | Boost.Beast, IXWebSocket, libwebsockets |
| `python-dotenv` | `kalshi_example.py` | Reads `.env` into environment variables | `getenv` plus a tiny parser, or systemd `EnvironmentFile=` |
| `requests` | `connection_utils.py` | Blocking HTTPS GET for market discovery | libcurl, cpr, or Beast HTTP |
| `pandas` | `connection_utils.py`, `kalshi_example.py` | Table for the series list, filtering, sorting | `std::vector` of structs, `std::sort` |
| `numpy` | `connection_utils.py` | Only used for `np.nan` | `std::numeric_limits<double>::quiet_NaN()` |
| `pytest`, `pytest-asyncio` | `tests/` (Phase 0) | Test runner, async test support | GoogleTest or Catch2 |

Two observations before diving in. First, `numpy` is imported for one constant, and `pandas` is used for a two-column table, so both are removable if you want a leaner collector. Second, the standard library does most of the heavy lifting (`asyncio`, `logging`, `argparse`, `zoneinfo`, `collections.Counter`), and those are what you actually have to reproduce in C++. Section 7 covers the load-bearing ones.

## 1. Kalshi mechanics you need first

Everything downstream (the book, imbalance, OFI, the log format) depends on a few facts about how Kalshi represents markets. All of the numbers below are checked against your `tests/fixtures/sample.jsonl`.

### 1.1 Yes bids and No bids

A Kalshi contract pays $1.00 if the event happens and $0.00 otherwise. The exchange publishes **two lists of bids**:

- `yes_dollars_fp`: resting orders to buy Yes at price `p`.
- `no_dollars_fp`: resting orders to buy No at price `q`. Buying No at `q` is economically the same as selling Yes at `1 - q`.

So in Yes terms, `best_bid = max(yes prices)` and `best_ask = 1 - max(no prices)`. In your sample, the TB market snapshot has a top Yes bid of 0.69 (74,014.19 contracts) and a top No bid of 0.30 (142,562.64 contracts). That is a Yes bid of 0.69 and a Yes ask of 0.70, a one-cent spread. The trades that follow print at `yes_price_dollars` 0.70, which matches.

The two markets of a game are nearly mirrors of each other. A Yes bid at 0.68 on `-TB` appears alongside a No bid at 0.68 on `-NYY` in the same millisecond (4,906 versus 4,697 contracts in the sample). Subscribing to both markets of a two-outcome event therefore stores close to the same information twice. Compression (section 9) absorbs most of that cost, so I would keep both, but it is a lever if storage ever bites.

### 1.2 Fixed-point strings

Prices arrive as strings with four decimals (`"0.1700"`), and quantities as strings with two decimals (`"1210036.64"`, `"-139.00"`). The `_fp` suffix means fixed-point. In every price you have recorded, the last two digits are zero, so the real tick appears to be one cent. Verify that over a full day before you hard-code it, because it decides whether your C++ book can use a 101-slot array or needs a 10,001-slot array (section 8).

Fixed-point strings exist so nobody does arithmetic in binary floating point. Your Python code keeps prices as strings (exact) and quantities as floats (inexact, hence the `round(..., 2)` in `apply_delta`). The C++ port should hold both as integers: price in ticks, quantity in hundredths.

### 1.3 `sid` and `seq`

The `subscribed` acknowledgements in the sample give `orderbook_delta` the subscription id `sid=1` and `trade` the id `sid=2`. Look at the `seq` field in the order book frames: it runs 1 through 6 across the six *snapshots* for six different markets, then continues 7, 8, 9... for deltas. So **`seq` counts frames per subscription, not per market**. For gap detection (your task 3.4) you keep one counter per `sid`, not one per ticker. The trade channel has its own independent `seq` starting at 1.

### 1.4 Bursts

The sample contains stretches such as `seq` 409 through about 560, where well over a hundred deltas arrive inside roughly six milliseconds with the same `ts_ms`, many on the same price level. That looks like a market maker cancelling and re-quoting a whole ladder at once. Three consequences:

- Your OFI is updated after every individual delta, so it sees intermediate book states that never existed as a coherent market (momentarily empty or crossed sides). Group deltas by identical `ts_ms` before computing OFI if you want the signal to reflect real events.
- A displayed rate of "upd/s" can spike by an order of magnitude and then vanish.
- Bursts are extremely repetitive, which is why general-purpose compression does so well on this data.

## 2. `cryptography`: signing the handshake

The signing code lives in `connection_utils.py`. The goal: prove to Kalshi that you hold the private key matching the public key registered to your access key, without sending the key.

### 2.1 What each call does

**`serialization.load_pem_private_key(data, password=None, backend=default_backend())`**

A PEM file is DER (a binary ASN.1 structure) base64-encoded between `-----BEGIN ... PRIVATE KEY-----` markers. The function decodes the base64, parses the ASN.1 into the RSA numbers (modulus `n`, public exponent `e`, private exponent `d`, primes `p` and `q`, and the CRT values), and returns an `RSAPrivateKey` object that wraps an OpenSSL key handle. `password=None` means the file must be unencrypted; an encrypted key raises `TypeError`. The `backend` argument has been ignored since `cryptography` 3.1, so `default_backend()` can be deleted.

**`private_key.sign(message, padding.PSS(...), hashes.SHA256())`**

This is RSA-PSS (RFC 8017). In order:

1. Hash the message with SHA-256, giving `mHash` (32 bytes).
2. Draw a **random salt** (here 32 bytes, because `salt_length=padding.PSS.DIGEST_LENGTH`).
3. Compute `H = SHA256(0x00*8 || mHash || salt)`.
4. Build a data block `DB = 0x00... || 0x01 || salt`, and XOR it with a mask generated from `H` by **MGF1** (a hash-based mask generator, here also SHA-256).
5. Assemble the encoded message `EM = maskedDB || H || 0xBC`.
6. Raise `EM` to the private exponent modulo `n`. The library does this with the Chinese Remainder Theorem (two half-size exponentiations using `p` and `q`), which is about four times faster than one full-size exponentiation.

The output has the same length as the key: 256 bytes for a 2048-bit key. Because of the random salt, **two signatures of the same message differ**. Never compare signatures for equality; verification recovers the salt and checks consistency. Signing takes on the order of a millisecond for a 2048-bit key, so it is irrelevant at one signature per connection attempt.

**`base64.b64encode(signature).decode("utf-8")`** turns the bytes into ASCII for an HTTP header (344 characters for 256 bytes, with no newlines).

**`InvalidSignature`** is raised by `verify()`, never by `sign()`. The `except InvalidSignature` branch in `sign_pss_text` can never run; it is dead code (your task 6.9).

### 2.2 What gets signed

`create_headers` signs `timestamp_ms + "GET" + path`, where `path` is `/trade-api/ws/v2` with any query string stripped (`path.split("?")[0]`). The three headers sent are `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, and `KALSHI-ACCESS-TIMESTAMP`. The timestamp is epoch milliseconds; `ZoneInfo` there has no effect, since an epoch value has no time zone. Kalshi rejects requests whose timestamp is too far from its own clock, so keep NTP synchronized (your task 4.10).

A fresh signature is built on every reconnect attempt, which is correct: a stale timestamp would be rejected.

### 2.3 C++ translation with OpenSSL 3

```cpp
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>
#include <chrono>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

struct KeyDeleter { void operator()(EVP_PKEY* k) const { EVP_PKEY_free(k); } };
using KeyPtr = std::unique_ptr<EVP_PKEY, KeyDeleter>;

KeyPtr load_private_key(const std::string& path) {
    std::unique_ptr<BIO, decltype(&BIO_free)> bio(BIO_new_file(path.c_str(), "r"), BIO_free);
    if (!bio) throw std::runtime_error("cannot open key file");
    EVP_PKEY* k = PEM_read_bio_PrivateKey(bio.get(), nullptr, nullptr, nullptr);
    if (!k) throw std::runtime_error("cannot parse PEM key");
    return KeyPtr(k);
}

std::string sign_pss_base64(EVP_PKEY* key, const std::string& text) {
    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> ctx(EVP_MD_CTX_new(), EVP_MD_CTX_free);
    EVP_PKEY_CTX* pctx = nullptr;  // owned by ctx, do not free
    if (EVP_DigestSignInit(ctx.get(), &pctx, EVP_sha256(), nullptr, key) != 1)
        throw std::runtime_error("DigestSignInit failed");
    EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING);   // not PKCS#1 v1.5
    EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST);  // 32 bytes for SHA-256
    EVP_PKEY_CTX_set_rsa_mgf1_md(pctx, EVP_sha256());            // MGF1 hash must match

    const auto* msg = reinterpret_cast<const unsigned char*>(text.data());
    size_t len = 0;
    EVP_DigestSign(ctx.get(), nullptr, &len, msg, text.size());  // ask for size
    std::vector<unsigned char> sig(len);
    if (EVP_DigestSign(ctx.get(), sig.data(), &len, msg, text.size()) != 1)
        throw std::runtime_error("sign failed");
    sig.resize(len);

    std::string out(4 * ((len + 2) / 3), '\0');
    int n = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(out.data()), sig.data(), static_cast<int>(len));
    out.resize(static_cast<size_t>(n));  // EVP_EncodeBlock adds no newlines
    return out;
}

std::string now_ms() {
    using namespace std::chrono;
    return std::to_string(duration_cast<milliseconds>(system_clock::now().time_since_epoch()).count());
}
// message = now_ms() + "GET" + "/trade-api/ws/v2"
```

Since your C++ client has authentication bugs, this is the checklist of the usual culprits, in rough order of likelihood:

1. **Wrong signed string.** It must be `timestamp + method + path` with no separators, an uppercase method, no host, no scheme, and no query string.
2. **Wrong padding.** OpenSSL defaults to PKCS#1 v1.5; Kalshi needs PSS.
3. **Wrong salt length.** Use the digest length (32), not the OpenSSL maximum.
4. **MGF1 hash mismatch.** SHA-1 is a common silent default in older code paths.
5. **Base64 with newlines.** `BIO_f_base64` inserts a newline every 64 characters unless you set `BIO_FLAGS_BASE64_NO_NL`. `EVP_EncodeBlock` does not.
6. **Timestamp in seconds, or taken from `steady_clock`.** It must be wall-clock epoch milliseconds.
7. **Headers missing from the upgrade request.** WebSocket has no authentication step after the handshake. The three headers must be on the initial HTTP `GET`.

The best test is cross-language: sign a fixed string in C++, then verify it in Python with `public_key.verify(sig, msg, PSS(...), SHA256())`. If Python raises `InvalidSignature`, the C++ parameters are wrong, and you can bisect them one at a time without involving Kalshi.

### 2.4 Getting the most out of it

- Keep the key outside the repo with mode `600`, and add `*.pem` and `*.key` to `.gitignore` (task 8.1).
- Load the key **once** at startup and reuse the object. In Python, do it inside `main()` rather than at import (your current module-level load crashes on import if the environment variable is unset).
- Time a signature. If you ever sign on the hot path, the cost matters; at one per connection it does not.

## 3. `websockets`: the connection

### 3.1 The protocol underneath

A WebSocket starts as an ordinary HTTP/1.1 request carrying `Upgrade: websocket` and a random `Sec-WebSocket-Key`. The server answers `101 Switching Protocols` with `Sec-WebSocket-Accept`, which is `base64(SHA1(key + a fixed GUID))`. After that the TCP connection carries **frames**: a few header bytes (final-fragment bit, opcode, mask bit, length) followed by the payload. Client-to-server frames are XOR-masked with a random 4-byte key; server-to-client frames are not. Opcodes you care about: text (`0x1`, what Kalshi sends), binary (`0x2`), close (`0x8`), ping (`0x9`), pong (`0xA`). For `wss://`, TLS wraps all of it.

### 3.2 Each call in your code

**`websockets.connect(url, additional_headers=..., ping_interval=10, ping_timeout=20, open_timeout=10)`** used as `async with`.

Awaiting it performs the TCP connect, the TLS handshake (a default context that verifies certificates and the hostname), and the HTTP upgrade, then returns a connection object. Leaving the `async with` block sends a close frame (code 1000), waits for the peer's close, and closes the socket.

- `additional_headers` adds HTTP headers to the **upgrade request**. This is where your signature goes. (The older legacy client called this `extra_headers`; the name changed when the asyncio implementation became the default.)
- `ping_interval=10`: a background task sends a ping every 10 seconds. `ping_timeout=20`: if no pong arrives within 20 seconds, the connection is closed with code 1011 and an exception is raised. The library also answers the server's pings for you automatically.
- `open_timeout=10` bounds TCP, TLS, and the HTTP upgrade together. Exceeding it raises `TimeoutError`, which is a subclass of `OSError`, so your `except OSError` clause catches it.

**`await websocket.send(text)`** frames the text, masks it, and waits for the write buffer to drain.

**`async for message in websocket`** calls `recv()` repeatedly. It reassembles fragmented messages and returns `str` for text frames. The critical detail:

- A *clean* close (`ConnectionClosedOK`) **ends the loop silently with no exception**.
- An *abnormal* close (`ConnectionClosedError`) raises.

That is why your `handle_messages` returns normally when the server hangs up, and why task 3.2 asks you to log the disconnect explicitly. (Also note that on a clean close your code sleeps 3 seconds inside the `try`, then 3 more after the `except` block, so that path waits 6 seconds, not 3.)

**Exceptions to know:** `ConnectionClosed` (parent of `ConnectionClosedOK` and `ConnectionClosedError`), `InvalidStatus` (the server answered the upgrade with something other than 101, such as 401 for bad credentials or 429 for rate limiting; **not** an `OSError`, so currently uncaught, per task 1.5), and `OSError` for network failures.

### 3.3 Defaults that bite

| Setting | Default | Why it matters here |
| --- | --- | --- |
| `max_size` | 1 MiB per message | A frame larger than this closes the connection with code 1009. Your snapshots are small, but pass a larger value if a deep book ever grows. |
| `max_queue` | 16 messages | If your code is slow to call `recv()`, the library stops reading the socket and TCP backpressure builds. A slow consumer can get disconnected by the server. Your receive loop only does `put_nowait`, so it stays fast. |
| `compression` | `"deflate"` | The permessage-deflate extension is negotiated by default. It saves bandwidth and burns CPU on both ends. For a high-rate feed of small frames, `compression=None` is often a better trade, and it matters for latency in the C++ client. |

### 3.4 The server side, and bugs in `tests/fake_server.py`

`websockets.asyncio.server.serve(handler, host, port)` runs `handler(ws)` once per client connection, and `ServerConnection.recv()` and `.send()` mirror the client. Three bugs in your fake server are worth fixing before you rely on it, because they make the fault modes unreliable:

- **`error` mode** calls `ws.send({...})` with a `dict`. `send` accepts only `str` or `bytes`, so this raises `TypeError`. Use `json.dumps(...)`.
- **`seqgap` and `badfield` modes** call `json.loads(frame)["seq"] = ...`. That mutates a throwaway object that is immediately discarded, so nothing changes on the wire. Parse into a variable, edit it, `json.dumps` it back, then send that.
- **Fault triggers** such as `i == random.randint(0, len(frames) - 1)` are re-rolled every iteration, so each frame has about a 1-in-N chance and the fault may never fire in a given run. Draw the index **once** before the loop.

### 3.5 C++ translation

Your repo's `.gitignore` already mentions `ixwebsocket` and `IXWebSocket-master`, so you have looked at one option. The realistic choices:

- **Boost.Beast** (on Asio): low level, excellent, and teaches the most. You assemble TCP, TLS, and WebSocket layers yourself.
- **IXWebSocket**: higher level. Callback API, automatic reconnection with backoff, built-in ping, per-message-deflate toggle.
- **libwebsockets / uWebSockets**: C, fast, steeper learning curve.

A synchronous Beast skeleton showing every layer that `websockets.connect` hides:

```cpp
#include <boost/beast.hpp>
#include <boost/asio/ssl.hpp>
#include <openssl/ssl.h>
#include <iostream>
namespace beast = boost::beast;
namespace http = beast::http;
namespace ws = beast::websocket;
namespace net = boost::asio;
namespace ssl = net::ssl;
using tcp = net::ip::tcp;

int main() {
    const std::string host = "external-api-ws.kalshi.com", port = "443", target = "/trade-api/ws/v2";
    net::io_context ioc;
    ssl::context ctx(ssl::context::tls_client);
    ctx.set_default_verify_paths();
    ctx.set_verify_mode(ssl::verify_peer);

    ws::stream<beast::ssl_stream<beast::tcp_stream>> stream(ioc, ctx);
    tcp::resolver resolver(ioc);
    auto results = resolver.resolve(host, port);
    beast::get_lowest_layer(stream).connect(results);                 // TCP
    SSL_set_tlsext_host_name(stream.next_layer().native_handle(), host.c_str());  // SNI
    stream.next_layer().handshake(ssl::stream_base::client);           // TLS

    stream.set_option(ws::stream_base::timeout{std::chrono::seconds(10),   // handshake
                                               std::chrono::seconds(30),   // idle
                                               true});                     // automatic keepalive pings
    stream.set_option(ws::stream_base::decorator([&](ws::request_type& req) {
        req.set("KALSHI-ACCESS-KEY", key_id);              // your three auth headers
        req.set("KALSHI-ACCESS-TIMESTAMP", ts);
        req.set("KALSHI-ACCESS-SIGNATURE", signature);
    }));
    stream.handshake(host, target);                                       // HTTP upgrade

    stream.write(net::buffer(R"({"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta","trade"],"market_tickers":["KXMLBGAME-26OCT072000TBNYY-TB"]}})"));
    beast::flat_buffer buf;
    for (;;) {
        stream.read(buf);                                               // one full message
        std::string_view msg(static_cast<const char*>(buf.data().data()), buf.size());
        // hand `msg` to your ring buffer here, then:
        buf.consume(buf.size());
    }
}
```

Points specific to the subscription bug you have reported:

- **Subscribe only after the handshake completes** (in IXWebSocket, inside the `Open` callback), and **resend the subscribe command after every reconnect**. A reconnect gives you a fresh connection with no subscriptions.
- Tickers are upper case; your Python code uppercases them before sending.
- Always read the `subscribed` acknowledgement and the `error` frames. A rejected ticker produces an `error` message, not a closed connection.
- Beast's `stream_base::timeout` handles handshake timeouts, idle timeouts, and keepalive pings for you, which replaces `ping_interval`, `ping_timeout`, and `open_timeout` in one option. Per-message deflate is **off by default** in Beast, which suits a latency-sensitive client.
- Callbacks in IXWebSocket run on the library's own thread. Push each message into your `RingBuffer` and return immediately; never parse or write to disk inside the callback.

## 4. `python-dotenv`: `load_dotenv(".env")`

`load_dotenv(path)` reads a file of `KEY=VALUE` lines (supporting quotes, `export` prefixes, `#` comments, and `${VAR}` interpolation) and copies each pair into `os.environ`. By default `override=False`, so a variable already set in the real environment **wins** over the file. That is what you want in production: systemd or the shell can override `.env`.

Two things to know. A relative path like `".env"` is resolved against the **current working directory**, which differs under cron and systemd (your task 1.6: anchor it to the script's own directory). And `os.getenv` returns `None` for a missing variable, so `load_private_key_from_file(None)` crashes with an unhelpful traceback; validate first and print a one-line error.

In C++ you do not need a library. Use `std::getenv` (it returns a null `const char*` when unset, so check it), and either parse `.env` yourself in about twenty lines or, better, let systemd inject the variables with `EnvironmentFile=` (section 13). Never log secret values.

## 5. `requests`: market discovery

### 5.1 What `requests.get(url, params=..., timeout=...)` does

1. Builds a `PreparedRequest`: URL-encodes `params` into the query string and adds default headers.
2. Takes a connection from a **urllib3 connection pool** (a fresh one for each bare `requests.get`, since no `Session` is reused), opens TCP and TLS with certificates from `certifi`, and sends the HTTP/1.1 request.
3. Blocks the calling thread until the response arrives, then returns a `Response` object.

The pieces you use: `r.status_code`, `r.headers` (a case-insensitive dictionary, which is how `Retry-After` is read), `r.json()`, and `r.raise_for_status()`, which raises `HTTPError` for any 4xx or 5xx.

**Timeouts.** Without `timeout=`, a request can wait forever. Your first call inside `get_series_list` has none (a bug). A single number sets both the connect and the read timeout; the read timeout is the gap between received bytes, not the total request time.

**Parameter encoding.** `params={"include_volume": True}` is encoded as `include_volume=True`, with a capital T. Many APIs expect lowercase `true`; check what Kalshi returns and use `str(flag).lower()` if volumes come back empty (task 6.3).

**Pagination.** `get_live_markets` follows the `cursor` field: each response includes a cursor for the next page until it is empty. That is standard cursor pagination, and your loop implements it correctly.

**Blocking.** `requests` is synchronous. Calling it inside a running asyncio loop stalls every other task, including the painter and the keepalive. That is fine at startup (it runs before `asyncio.run`), but it is why task 8.4 says to wrap periodic refreshes in `asyncio.to_thread`.

### 5.2 Using it well

Create one `requests.Session()` and reuse it for all calls: it keeps the TLS connection open and shaves a handshake off each request. Set a timeout on every call, honour `Retry-After`, and apply exponential backoff with jitter. Your `get_series_list` retry loop calls `requests.get(URL, ...)` (the markets endpoint) instead of `SERIES_URL`, which is bug 6.1.

### 5.3 C++ translation

Use **libcurl** (directly, or through the `cpr` wrapper). The mapping is direct:

```cpp
CURL* c = curl_easy_init();
curl_easy_setopt(c, CURLOPT_URL, url.c_str());          // include the query string yourself
curl_easy_setopt(c, CURLOPT_TIMEOUT, 10L);              // total timeout in seconds
curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, write_cb);   // append bytes to a std::string
curl_easy_setopt(c, CURLOPT_WRITEDATA, &body);
CURLcode rc = curl_easy_perform(c);
long status = 0;
curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, &status);
curl_easy_cleanup(c);
```

Parse the JSON with `nlohmann/json` (convenient) or `simdjson` (fast). Reuse one `CURL*` handle across requests to get connection reuse, the equivalent of a `Session`.

## 6. `pandas` and `numpy`

### 6.1 What your code uses

- **`pd.DataFrame({"tickers": tickers, "volume": volume})`** builds a table. Internally pandas stores each column as a contiguous array (NumPy for numbers, Python objects or Arrow strings for text).
- **`df.sort_values("volume")`** returns a reordered copy. It is **ascending**, so the biggest series come last (task 6.5).
- **`df[df["volume"] > min_volume]`** is boolean indexing: the comparison yields a column of `True`/`False`, and indexing with it keeps the `True` rows. Rows with `NaN` volume compare as `False` and drop out.
- **`.reset_index(drop=True)`** renumbers the rows 0, 1, 2... and discards the old index.
- **`df[["tickers"]]`** (double brackets) keeps a one-column **DataFrame**, while `df["tickers"]` would be a **Series**. Your function returns a DataFrame when `include_volume` is true and a *different shape* otherwise, which is bug 6.2.
- **`series["tickers"].str.contains(r"GAME|MATCH")`** runs a regular-expression search on every string and returns a boolean column.

**`numpy`** appears only as `np.nan`, the IEEE 754 "not a number" value. `float("nan")` is identical, so `numpy` can be dropped. Remember the rules: `nan` compares `False` to everything (including itself), which is exactly why NaN rows vanish from a `> min_volume` filter.

### 6.2 C++ and the honest recommendation

A `std::vector<struct { std::string ticker; double volume; }>`, `std::sort` with a comparator, and `std::erase_if` replicate all of this in a few lines. For the regex, `std::regex` is slow; for `GAME|MATCH` just use `std::string::find` twice, or RE2 or PCRE2 if you need real patterns. NaN is `std::numeric_limits<double>::quiet_NaN()` and behaves the same way.

For the Python collector, a list of dictionaries and `sorted(..., key=...)` would do the same job, and removing `pandas` and `numpy` saves several hundred milliseconds of startup time and substantial memory. Keep `pandas` for the **research** side of the project, where it earns its place.

## 7. The standard-library machinery your code leans on

These are not third-party, but the whole architecture rests on them, and each one has a different shape in C++.

### 7.1 `asyncio`

`asyncio.run(main())` creates an **event loop**: a single thread that keeps a list of ready tasks and runs each one until it hits an `await`, where it hands control back. Nothing ever runs in parallel; tasks interleave only at `await` points. Your collector has four tasks per process: the receive loop (`handle_messages`), `writer_task`, `display_loop`, and `error_data_loop`.

That explains the claim in your README that no locks are needed. `apply_delta` and `OFITracker.update` contain no `await`, so no other task can observe a half-updated book. **That guarantee evaporates the moment you use threads**, which is the main design difference when you port to C++ (section 8).

Key behaviours:

- **`asyncio.Queue()` is unbounded by default.** If the writer stalls, `put_nowait` keeps succeeding and memory grows without limit. That is why task 4.3 asks you to watch `qsize()`.
- **`create_task` results must be kept and observed.** If a task raises and nobody awaits it, the exception is only reported when the task object is garbage-collected, usually as a vague "Task exception was never retrieved". A dead writer therefore looks like a healthy program (your task 1.2).
- **`writer_task` does blocking file I/O on the event loop thread** (the `# noqa: ASYNC230` comment is the linter warning you silenced). A slow disk or a hung external drive freezes the receive loop, the painter, and the keepalive pings together. In Python, move writes to a thread (`asyncio.to_thread` or a dedicated `threading.Thread` consuming a `queue.Queue`). In C++ this is exactly why the writer gets its own thread.
- **`asyncio.sleep(n)`** yields to the loop for `n` seconds; it never blocks other tasks, unlike `time.sleep`.

### 7.2 Python construct to C++ equivalent

| Python | Role | C++ equivalent |
| --- | --- | --- |
| `asyncio` event loop | One thread multiplexing I/O | `boost::asio::io_context` (single thread), or `epoll` directly |
| `async with` / `async for` | Suspend until the socket is ready | Beast async calls with completion handlers, or C++20 `net::awaitable` coroutines |
| `asyncio.Queue` | Hand data between tasks | Lock-free SPSC ring buffer (your `RingBuffer`), bounded |
| `time.time_ns()` | Wall-clock receive timestamp | `clock_gettime(CLOCK_REALTIME)`, or `system_clock::now()` |
| `logging` | Application log | spdlog, or `fprintf` to stderr under systemd (journald captures it) |
| `argparse` | CLI flags | CLI11, or `cxxopts` |
| `collections.Counter` | Error counters | `std::unordered_map<std::string, uint64_t>` or fixed `std::atomic<uint64_t>` fields |
| `dataclass` | Plain data holder | `struct` |
| `zoneinfo` + `datetime` | New York filenames | Avoid it: use UTC (`std::chrono::sys_time`, `gmtime_r`) |

### 7.3 Two clocks, two purposes

`time.time_ns()` is the wall clock. It can jump when NTP steps it, and request signing also depends on it. For *measuring durations* (latency between receive and processing, how long a reconnect took), use `time.monotonic_ns()` (C++: `steady_clock`). For research, store **both** your receive time and the exchange's `ts_ms`; their difference is your network-plus-queue latency, and watching it drift is a free health metric.

### 7.4 Logging at import time

`logging.basicConfig(filename=f"logs/kalshi_example_{date}.log")` computes the date once, when the module imports. A process that runs past midnight keeps writing yesterday's file. Use `TimedRotatingFileHandler`, or log to stderr and let systemd's journal handle rotation (section 12).

## 8. Porting the collector to C++

### 8.1 Architecture

The Python program is one thread with cooperative tasks. The natural C++ shape uses three threads and two hand-off structures:

1. **Network thread** (runs the Beast or IXWebSocket loop). For each frame: stamp `recv_ns`, copy the raw bytes into the **writer ring**, parse, and update the order books. It must never touch the disk or the terminal.
2. **Writer thread.** Pops frames from the ring, appends them to a buffered file, flushes about once a second, rotates by hour, and (optionally) compresses. A stalled disk then delays only this thread.
3. **Display thread.** Wakes 10 times a second and prints. It must **not** read the live order book, because the network thread is mutating it. Instead the network thread publishes a small plain struct per market (best bid, best ask, imbalance, OFI, update counter) through a **seqlock** or a double buffer, and the display thread reads that.

The writer ring is exactly the structure you are already building in your `RingBuffer` project. A minimal correct single-producer single-consumer version:

```cpp
#include <array>
#include <atomic>
#include <cstddef>

template <typename T, std::size_t N>   // N must be a power of two
class SpscRing {
    static_assert((N & (N - 1)) == 0, "N must be a power of two");
    alignas(64) std::atomic<std::size_t> head_{0};   // advanced by the consumer
    alignas(64) std::atomic<std::size_t> tail_{0};   // advanced by the producer
    std::array<T, N> buf_;
public:
    bool try_push(const T& v) {                       // producer thread only
        std::size_t t = tail_.load(std::memory_order_relaxed);
        if (t - head_.load(std::memory_order_acquire) == N) return false;   // full
        buf_[t & (N - 1)] = v;
        tail_.store(t + 1, std::memory_order_release);
        return true;
    }
    bool try_pop(T& out) {                            // consumer thread only
        std::size_t h = head_.load(std::memory_order_relaxed);
        if (h == tail_.load(std::memory_order_acquire)) return false;       // empty
        out = buf_[h & (N - 1)];
        head_.store(h + 1, std::memory_order_release);
        return true;
    }
};
```

The two indices only ever increase and wrap naturally as unsigned integers, so `tail - head` is always the fill level. The `alignas(64)` keeps each index on its own cache line, so the two threads do not fight over one (false sharing). Acquire/release ordering is what makes the data written to `buf_` visible before the index that announces it.

Two design decisions this forces that Python let you ignore:

- **The ring is bounded.** Decide the full-queue policy now. For a collector, never drop silently: increment a counter and write a `gap` marker into the data stream, so replay knows data is missing. Make the ring large (for example 2^20 slots) so that a normal disk hiccup is absorbed.
- **Slots must hold the frame.** Use a fixed-size slot such as `struct Frame { int64_t recv_ns; uint32_t len; char data[N]; }` (check the largest snapshot you have seen), or a pool of reusable buffers with indices passed through the ring.

### 8.2 The order book

Python uses a `dict[str, float]` per side and recomputes `max(...)` over the keys, converting strings to floats, on every call. That is O(levels) per query. In C++ you can do better because prices live on a tiny, fixed grid.

- **Prices as integers.** Parse `"0.1700"` into integer ticks (1/10000 of a dollar, or 1/100 if a full day confirms cent-only ticks).
- **Quantities as integers.** Parse `"-139.00"` into hundredths (`-13900`). Integer arithmetic has no drift, which removes your tasks 2.1 and 2.5 entirely.
- **A flat array indexed by price**, plus a bitmap of non-empty levels and a cached best index. Updates are O(1); when the best level empties, the bitmap finds the next one in a few word operations.

```cpp
#include <array>
#include <bit>
#include <cstdint>

class BookSide {
    static constexpr int kN = 10001;                     // ticks 0..10000 = $0.0000..$1.0000
    std::array<std::int64_t, kN> qty_{};                 // hundredths of a contract
    std::array<std::uint64_t, (kN + 63) / 64> bits_{};   // bit set <=> qty_[px] > 0
    int best_ = -1;                                      // highest non-empty price, -1 if empty

    int highest_below(int px) const {                    // highest set index < px, or -1
        if (px <= 0) return -1;
        int i = px - 1, w = i >> 6;
        std::uint64_t m = bits_[w] & (~0ULL >> (63 - (i & 63)));
        for (;;) {
            if (m) return (w << 6) + 63 - std::countl_zero(m);
            if (--w < 0) return -1;
            m = bits_[w];
        }
    }
public:
    void clear() { qty_.fill(0); bits_.fill(0); best_ = -1; }
    void set_level(int px, std::int64_t q) { apply(px, q - qty_[px]); }   // used for snapshots
    void apply(int px, std::int64_t delta) {
        std::int64_t q = qty_[px] + delta;
        if (q <= 0) {
            qty_[px] = 0;
            bits_[px >> 6] &= ~(1ULL << (px & 63));
            if (px == best_) best_ = highest_below(px);
        } else {
            qty_[px] = q;
            bits_[px >> 6] |= (1ULL << (px & 63));
            if (px > best_) best_ = px;
        }
    }
    int best_px() const { return best_; }
    std::int64_t best_qty() const { return best_ < 0 ? 0 : qty_[best_]; }
};
// Yes ask in ticks = 10000 - no.best_px(); quantity = no.best_qty().
```

Memory: 80 KB per side per market at the 10,001-slot size, so about 40 MB for 240 markets, which is fine. At cent resolution it is a few hundred bytes. Use `std::array` inside a `std::vector<MarketState>` indexed by a market number looked up **once** from the ticker (`unordered_map<string, uint16_t>`), not per message.

Parsing decimals without `std::stod` (which is slow and goes through floating point):

```cpp
#include <cstdint>
#include <string_view>

// Parse "-1210036.64" with `scale` decimals into integer units of 10^-scale.
// Assumes well-formed input; validate (digits only, <= scale decimals) in production.
std::int64_t parse_fixed(std::string_view s, int scale) {
    std::size_t i = 0;
    bool neg = !s.empty() && s[0] == '-';
    if (neg) ++i;
    std::int64_t whole = 0;
    for (; i < s.size() && s[i] != '.'; ++i) whole = whole * 10 + (s[i] - '0');
    std::int64_t frac = 0;
    int digits = 0;
    if (i < s.size() && s[i] == '.')
        for (++i; i < s.size() && digits < scale; ++i, ++digits) frac = frac * 10 + (s[i] - '0');
    for (; digits < scale; ++digits) frac *= 10;
    std::int64_t p = 1;
    for (int k = 0; k < scale; ++k) p *= 10;
    std::int64_t v = whole * p + frac;
    return neg ? -v : v;
}
// price_ticks = parse_fixed("0.1700", 4); dqty = parse_fixed("-139.00", 2);
```

For JSON use **simdjson**'s On Demand API: it parses lazily as you pull named fields out, and avoids building a tree. It requires the input buffer to have extra padding bytes, so give your frame slots a little slack.

### 8.3 OFI and imbalance in C++

Your `OFITracker` translates almost line for line, with two corrections that Python hides:

- Store the previous state as integers `(bid_px, bid_qty, ask_px, ask_qty)`, and reset `prev` to "none" whenever either side is empty (your task 2.7).
- Decide whether to update OFI per delta or per group of deltas sharing one `ts_ms` (section 1.4). Per-group updates remove the artificial intermediate states from re-quote bursts.

### 8.4 Build, tests, and repository hygiene

- **Build system:** CMake with `find_package` for Boost, OpenSSL, CURL, simdjson, and zstd, with dependencies provided by vcpkg or Conan. Use `-O2 -Wall -Wextra` normally, and a debug preset with `-fsanitize=address,undefined` (plus a separate `-fsanitize=thread` run once you have threads).
- **Reuse your Python fixtures.** Feed `tests/fixtures/sample.jsonl` to the C++ book, dump the final best bid, best ask, and level counts for each market, and compare them against the Python replay. Matching output across languages is the strongest correctness test you can write (task 5.4 in cross-language form).
- **Your `.gitignore` will silently hide C++ project files.** The patterns `*.txt`, `*.hpp`, `*.json`, and `*.toml` match `CMakeLists.txt`, every `.hpp` header, `vcpkg.json` and `CMakePresets.json`, and `pyproject.toml` (the file task 0.1 asks you to add). Add re-include lines after the ignore rules:

```gitignore
!CMakeLists.txt
!requirements.txt
!vcpkg.json
!CMakePresets.json
!pyproject.toml
!*.hpp
```

Then check with `git check-ignore -v CMakeLists.txt` (it should print nothing).

- **Line endings.** Your `.gitattributes` forces LF for `*.js` and `*.py` but not for shell scripts. A `copy_files.sh` checked out with CRLF fails with `bad interpreter: /bin/bash^M`. Add:

```gitattributes
*.sh       text eol=lf
*.service  text eol=lf
*.timer    text eol=lf
*.cpp      text eol=lf
*.hpp      text eol=lf
*.h        text eol=lf
CMakeLists.txt text eol=lf
```

## 9. Compression

### 9.1 Where the bytes go

A typical delta line in your log is about 400 bytes. The useful content is a market, a side, a price, a signed quantity, and a few timestamps, which fits in roughly 15 to 30 bytes if stored as binary integers. The rest is overhead, and most of it is in three places:

- **Double encoding.** The frame is a JSON string inside a JSON object, so every inner quote becomes `\"`. That adds characters and forces two parses on read.
- **Repeated field names and constants** in every line (`"market_ticker"`, `"price_dollars"`, the 36-character `market_id` UUID, `"sending_ts_ms"`).
- **Redundant timestamps.** Each delta carries `ts` (an ISO string) and `ts_ms` (the same instant as an integer), plus `sending_ts_ms`, plus your own `timestamp`.

All of this is highly repetitive, and the re-quote bursts in section 1.4 repeat the same level and quantity many times. That is the ideal input for a general-purpose compressor, so even the unmodified log will shrink dramatically. Treat my "15 to 30 bytes" as an estimate; measure the real ratio on your own data (next section) before you plan disk budgets.

### 9.2 Measure first

Take one real hour file and compare codecs on **ratio and time**:

```bash
f=data/20261007/orderbook_20.txt
ls -l "$f"
for lvl in 1 3 9 19; do
  echo -n "zstd -$lvl: "; ( time zstd -$lvl -T0 -q -c "$f" | wc -c ) 2>&1 | tr '\n' ' '; echo
done
echo -n "zstd -19 --long=27: "; zstd -19 --long=27 -T0 -q -c "$f" | wc -c
echo -n "xz -9: ";   xz -9 -T0 -c "$f" | wc -c
echo -n "gzip -6: "; gzip -6 -c "$f" | wc -c
```

Qualitatively: **lz4** is the fastest with the lowest ratio; **gzip/zip (deflate)**, which is what `copy_files.sh` uses today, is the old middle ground; **zstd** at low levels (1 to 5) is fast and already beats deflate on ratio; **zstd** at high levels (15 to 19, with `--long`) and **xz** give the smallest output but cost far more CPU. On a low-power machine, the CPU budget matters: compress lightly while recording and heavily at night.

### 9.3 A layered strategy

Do not choose one format. Use three tiers, each with a clear job.

**Tier 1: raw archive (the source of truth).** Keep the exact bytes you received, compressed. This is the only copy that lets you re-derive anything later if you discover a parsing bug. Format: one file per hour, `.zst`, written by the collector or compressed by the nightly job.

**Tier 2: columnar research tables (derived).** Convert the raw archive to Parquet with ZSTD compression using DuckDB, which you already use. Sorting by `(ticker, recv_ns)` before writing makes timestamps delta-encode well and lets queries skip whole row groups. Parquet is **rebuildable** from Tier 1, so a bug in the conversion never costs you data.

**Tier 3 (optional, for the C++ port): a compact binary replay format.** Only worth building if replay speed in C++ matters.

### 9.4 Tier 1 in practice

**Cheapest first step: stop double-encoding.** Write each record as `recv_ns<TAB>frame` instead of a JSON wrapper:

```text
1791427623721469309\t{"type":"orderbook_delta","sid":1,"seq":7,"msg":{...}}
```

This is byte-exact (no escaping), needs one split and one parse to read, and compresses better. One catch visible in your fixture: **each Kalshi frame ends with a newline character**. Strip it before writing (and assert that the remaining frame contains no embedded newline), or your line format breaks. If you change the line format, change the writer, `replay_json`, `check_file`, and the README together (task 4.4), and measure the size difference first.

**Compress while writing.** Two ways, with different dependencies:

- **No new dependency:** the standard-library `gzip` module. `gzip.open(path, "at")` appends a new gzip member each time you open it, and concatenated gzip members are valid, so a crash costs only the unfinished tail.
- **Better ratio:** zstd. In Python 3.12 that is the third-party `zstandard` package (the standard library gets `compression.zstd` only in 3.14). In C++ it is `libzstd`: create one `ZSTD_CCtx`, call `ZSTD_compressStream2` with `ZSTD_e_continue` as data arrives, and call it with `ZSTD_e_flush` about once a second. Concatenated zstd frames are valid, and each flush makes everything written so far decodable.

The flush interval is a trade-off you control: flushing every second costs a little ratio and means `kill -9` loses at most about a second, which matches the target in task 4.2.

**Dictionaries.** If you compress small independent blocks, train a dictionary on sample data: `zstd --train sample_lines/* -o kalshi.dict`, then `zstd -D kalshi.dict`. Because the field names and structure repeat in every line, a dictionary recovers most of the ratio you would otherwise lose on small blocks.

**Nightly high-compression pass.** Let the collector write fast, low-level zstd; then recompress yesterday's files at a high level overnight, with the work deprioritized:

```bash
nice -n 19 ionice -c3 zstd -19 --long=27 -T0 --rm -q "$file"
```

Verify before removing the original: `zstd -t file.zst`, and compare `zstd -dc file.zst | wc -l` against the original line count.

### 9.5 Tier 2 in practice: DuckDB to Parquet

For your current double-encoded format:

```sql
CREATE TABLE deltas AS
SELECT
  "timestamp"                                                    AS recv_ns,
  json_extract_string(msg, '$.msg.market_ticker')                AS ticker,
  CAST(json_extract_string(msg, '$.seq') AS BIGINT)              AS seq,
  json_extract_string(msg, '$.msg.side')                         AS side,
  CAST(round(CAST(json_extract_string(msg, '$.msg.price_dollars') AS DOUBLE) * 10000) AS INTEGER) AS px_ticks,
  CAST(round(CAST(json_extract_string(msg, '$.msg.delta_fp') AS DOUBLE) * 100) AS BIGINT)        AS dqty,
  CAST(json_extract_string(msg, '$.msg.ts_ms') AS BIGINT)        AS exch_ts_ms
FROM read_json('data/20261007/orderbook_*.txt',
               format = 'newline_delimited',
               columns = {"timestamp": 'UBIGINT', msg: 'VARCHAR'})
WHERE json_extract_string(msg, '$.type') = 'orderbook_delta';

COPY (SELECT * FROM deltas ORDER BY ticker, recv_ns)
TO 'deltas_20261007.parquet'
(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 500000);
```

Do the same for `trade` and `orderbook_snapshot` records into their own tables. Store integers (ticks and hundredths), not floats, so your research code inherits the exact-arithmetic discipline of the C++ book. Lines with a connect/disconnect marker (task 3.3) have no `msg` key, so the `WHERE` clause skips them; load them into a separate `events` table so you can tell where the gaps are.

### 9.6 Tier 3: a compact record for C++

If you build one, keep it simple, versioned, and self-describing:

```cpp
#pragma pack(push, 1)
struct DeltaRec {            // 28 bytes
    std::uint64_t recv_ns;
    std::uint32_t seq;
    std::uint16_t market;    // index into the per-file market table
    std::uint8_t  kind_side; // low bit: side (0 yes, 1 no); high bits: record type
    std::uint8_t  flags;
    std::int32_t  px_ticks;
    std::int64_t  dqty;      // hundredths of a contract
};
#pragma pack(pop)
```

Put a file header first (magic bytes, format version, and a table mapping `market` index to ticker and `market_id`), group records into blocks of tens of thousands, add a CRC32C to each block, and compress each block with zstd. For better ratio, store columns together (all `recv_ns` deltas, then all prices) rather than interleaved records, because zstd finds far more redundancy in a column of similar numbers. Never drop unknown fields here without a Tier 1 raw copy to fall back on.

### 9.7 Capacity planning

Measure rather than guess:

1. `bytes_per_hour` from a representative evening of real data (games make the busy hours very different from quiet overnight hours).
2. `daily = sum of the 24 hourly sizes`.
3. `compressed_daily = daily / ratio`, using the ratio from your benchmark.
4. `days_of_space = free_bytes / compressed_daily`, computed separately for the local disk and the external drive.

Print that estimate at startup and refuse to start (or warn loudly) if fewer than N days remain (task 4.9). Remember that flash drives are cheap but have limited write endurance and no redundancy; compression also reduces the writes they absorb.

## 10. Data management

The goal is a pipeline where you can answer three questions at any time: *what exactly did I record, is it intact, and where are the copies?*

### 10.1 Layout and naming

- **Use UTC for directory and file names.** Your collector names files by America/New\_York date and hour, while `copy_files.sh` uses the system time zone for "yesterday". Those can disagree, and the November fall-back hour maps two different hours onto one filename. UTC has neither problem (task 4.5). Keep the New York conversion in your research code, where it belongs.
- **One file per hour**, in a per-day directory, as you have now: `data/YYYYMMDD/orderbook_HH.<ext>`. It is a good unit: small enough to verify and re-run, large enough to avoid millions of tiny files.
- **Derived data lives elsewhere** and is partitioned for the queries you run: `parquet/deltas/date=YYYY-MM-DD/part.parquet`. DuckDB prunes partitions from the path.
- **Never mix raw and derived** in one tree. Raw is write-once; derived can be deleted and rebuilt.

### 10.2 Write-once and atomic

- Write the current hour to `orderbook_HH.<ext>.partial`, and `rename` it to its final name only when the hour closes cleanly. `rename` within one filesystem is atomic, so any file without `.partial` is complete. The nightly job then needs no guesswork about whether a file is still open (my script in section 11 also checks modification times as a second guard).
- Treat finished files as **immutable**. If you must fix something, write a new derived file; never edit the original.
- Make every conversion step **idempotent**: write to a temporary name, then rename. Re-running a day should give identical output.

### 10.3 What "written" means

A `write()` or Python `f.write()` puts bytes in a user-space buffer. `flush()` moves them to the kernel, which survives a *process* crash. `fsync()` forces them onto the device, which survives *power loss*. You flush about once a second (task 4.2). For `fsync`, calling it once per second on a busy file is cheap on an SSD but can be slow on a flash drive; do it at file close and rotation unless power loss is a real risk for you (task 4.12).

### 10.4 Manifest: an index of what you have

Maintain a small table (SQLite, or a DuckDB table in the same database you use for research) with one row per raw file:

| Column | Purpose |
| --- | --- |
| `path`, `bytes` | Where it is, how big |
| `sha256_uncompressed` | Checksum of the decompressed content, so recompressing later does not invalidate it |
| `n_lines`, `n_markets` | Quick sanity numbers |
| `first_recv_ns`, `last_recv_ns` | Coverage in time |
| `n_connections`, `n_seq_gaps`, `n_crossed` | Quality flags from `check_file` (task 5.3) |
| `archived_at`, `archive_sha256` | Whether and when it was copied off the machine |

Having this lets the nightly job, your research code, and you all ask "is 2026-10-07 hour 20 good?" in one query.

### 10.5 Record what the order book cannot tell you

Order book messages say nothing about **who won** or which markets were *supposed* to be in the subscription. Write two small side files each day:

- **Subscription list at startup:** the exact tickers requested, with a timestamp. Compare it to which tickers actually produced snapshots (task 3.10).
- **Market metadata snapshot:** the raw JSON from the markets REST endpoint (ticker, event, status, close time, and the settlement result once known). Look at the fields the endpoint returns today and keep the **raw response**, compressed. For backtesting you need settlement outcomes to label events, and you can only fetch them reliably after the market resolves, so schedule a second pull a day or two later.

### 10.6 Gaps are data

Your reconnects discard the books and restart OFI. The connect and disconnect marker lines (task 3.3), the per-subscription `seq` gap markers (task 3.4), and the ring-full drop marker from the C++ design (section 8.1) are what let research code exclude affected intervals. Without them a missing hour looks like a quiet hour. A daily quality report built from the manifest (lines, snapshots per market per connection, gaps, crossed-book count) is the first thing to look at each morning.

### 10.7 Retention and tiers

| Tier | Where | Format | Keep |
| --- | --- | --- | --- |
| Hot | Local internal disk | Current and recent days, lightly compressed | A few days (`KEEP_DAYS`) |
| Warm | External drive | Daily `.tar.zst` plus `.sha256` | Long term |
| Research | Local disk | Parquet, DuckDB | Rebuildable; keep what you query |

The rule that protects you: **delete the local raw copy only after the external copy has been read back and its checksum matched.** The script below enforces it with a marker file.

### 10.8 The external drive

Your copy target is a flash drive mounted at `/d`. Several flash-drive specifics matter:

- **Filesystem.** If it is FAT32 or exFAT (common for thumb drives), there are no Unix permissions, owners, or symlinks, and FAT32 has a **4 GiB file size limit** (a large daily archive can hit it). `rsync -a` tries to preserve permissions and ownership and prints errors on such drives, so use `-rt --no-perms --no-owner --no-group` instead. FAT timestamps have two-second resolution, so add `--modify-window=2` or rsync will think files differ every run.
- **Detect a missing drive.** If `/d` is not mounted, `mkdir -p /d/...` quietly creates directories on the **root filesystem** and slowly fills the system disk. Always test `mountpoint -q /d` first.
- **Mount options** in `/etc/fstab`: `nofail` (boot continues if the drive is absent), `x-systemd.device-timeout=5`, and `noatime`. Mount by UUID, not `/dev/sdX`, because device letters change.
- **Unplugging.** Run `sync` after writing, and unmount before removal.
- **Reliability.** A single flash drive is a single point of failure and a poor archive on its own. Keep a second copy somewhere else (a second drive, or object storage via `rclone copy --checksum`), following the 3-2-1 rule: three copies, two media types, one off-site. Compression helps here too, since fewer bytes means fewer writes to a device with limited endurance.

### 10.9 The local disk

- Put `data/` on its own partition or directory quota if you can, so a runaway log cannot starve the operating system.
- Mount with `noatime` (reads should not cause writes).
- Watch free space with an alert at, say, 20 percent, not at 100 percent.
- Prune application logs with `logrotate` or journald limits (`SystemMaxUse=`), not by hand.

## 11. Shell scripting

### 11.1 What is wrong with `copy_files.sh` today

| Line | Problem | Consequence |
| --- | --- | --- |
| `rsync -av --remove-source-files "${ZIP_FILE}/" ...` | A trailing slash on a **file** makes rsync treat it as a directory | The copy fails |
| same line | `--remove-source-files` deletes the zip as soon as rsync finishes | The planned checksum can never run; if the copy is corrupt, the only copy is gone |
| `mkdir -p "$DATA_DEST"` | Never checks that `/d` is mounted | With the drive unplugged it creates `/d` on the system disk |
| `on_err`: `log "$err_msg" >&2` | `log` expects the level first, so the message is treated as the level | A garbled log line |
| `on_err`: `mail -s "..."` | No recipient address | `mail` fails, so you never get the alert |
| `die` plus `trap ERR` | Two alert paths | Possible duplicate emails |
| `date -d 'yesterday'` | System time zone, while the collector names directories in New York time | Wrong or missing directory |
| Email address in the script | Personal data committed to the repo | Move it to an environment variable |
| Mixed `zip -r` of already-compressible text | Deflate only | Lower ratio than zstd |

### 11.2 Principles worth adopting

- **Strict mode:** `set -Eeuo pipefail`. `-e` exits on error, `-u` treats unset variables as errors, `pipefail` makes a pipeline fail if any stage fails, and `-E` makes the `ERR` trap fire inside functions too.
- **One exit path for alerts.** An `ERR` trap records *where* it failed, and a single `EXIT` trap decides whether to mail. That gives exactly one email per failure.
- **Quote every expansion** (`"$var"`), use `[[ ... ]]` for tests, and use `--` before file arguments so a name starting with `-` cannot be mistaken for an option.
- **Make it safe to re-run.** If the day is already archived and verified, do nothing.
- **Lock it** with `flock` so two runs cannot overlap.
- **Verify, then delete.** Never remove a source until the destination has been read back and checked.
- **Provide a dry run** and run `shellcheck` and `bash -n` before every commit.
- **No secrets or addresses in the script.** Read them from the environment (systemd's `EnvironmentFile=`).

### 11.3 A revised archive script

Save as `archive_day.sh`, `chmod +x`, and keep it with LF line endings (section 8.4).

```bash
#!/usr/bin/env bash
# archive_day.sh - compress one day of collector data, copy it to the external drive,
# verify the copy by checksum, and only then allow local pruning.
# Usage: archive_day.sh [YYYYMMDD]     (default: yesterday, New York time, matching the collector)
# Environment: DATA_ROOT DEST_ROOT FALLBACK_ROOT KEEP_DAYS ZSTD_LEVEL ALERT_TO DRY_RUN=1
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"

DATA_ROOT="${DATA_ROOT:-$SCRIPT_DIR/data}"
DEST_ROOT="${DEST_ROOT:-/d}"
FALLBACK_ROOT="${FALLBACK_ROOT:-$SCRIPT_DIR/archive_pending}"
KEEP_DAYS="${KEEP_DAYS:-7}"
ZSTD_LEVEL="${ZSTD_LEVEL:-12}"
ALERT_TO="${ALERT_TO:-}"
DRY_RUN="${DRY_RUN:-0}"
LOCK_FILE="${LOCK_FILE:-${XDG_RUNTIME_DIR:-/tmp}/kalshi-archive.lock}"

TMP_DIR=""; FAIL_MSG=""; ERR_INFO=""; DAY=""

log()  { printf '[%s] %-7s %s\n' "$(date '+%F %T')" "$1" "${*:2}"; }
info() { log INFO "$@"; }
warn() { log WARNING "$@" >&2; }
die()  { FAIL_MSG="$*"; log ERROR "$*" >&2; exit 1; }
run()  { if [[ "$DRY_RUN" == 1 ]]; then info "DRY-RUN: $*"; else "$@"; fi; }

notify() {   # notify SUBJECT BODY
    if [[ -z "$ALERT_TO" ]]; then warn "ALERT_TO is unset; not sending: $1"; return 0; fi
    printf '%s\n' "$2" | mail -s "$1" "$ALERT_TO" || warn "mail command failed"
}

trap 'ERR_INFO="line $LINENO: $BASH_COMMAND"' ERR
on_exit() {
    local rc=$?
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then rm -rf -- "$TMP_DIR"; fi
    if (( rc != 0 )); then
        notify "[${HOSTNAME:-host}] $SCRIPT_NAME FAILED for ${DAY:-?}" \
               "${FAIL_MSG:-unexpected failure at ${ERR_INFO:-unknown location}} (exit $rc)"
    fi
}
trap on_exit EXIT

prune_old() {   # remove local days older than KEEP_DAYS, but only if verified-archived
    local d
    while IFS= read -r -d '' d; do
        if [[ -f "$d/.archived" ]]; then
            info "pruning verified day $(basename -- "$d")"
            run rm -rf -- "$d"
        else
            warn "keeping $(basename -- "$d"): older than $KEEP_DAYS days but not verified-archived"
        fi
    done < <(find "$DATA_ROOT" -mindepth 1 -maxdepth 1 -type d \
                  -name '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]' -mtime +"$KEEP_DAYS" -print0)
}

# ---- single instance ---------------------------------------------------------
exec 9>"$LOCK_FILE"
flock -n 9 || { info "another instance holds the lock; exiting"; exit 0; }

# ---- preflight ----------------------------------------------------------------
DAY="${1:-$(TZ=America/New_York date -d yesterday +%Y%m%d)}"
[[ "$DAY" =~ ^[0-9]{8}$ ]] || die "bad day '$DAY' (expected YYYYMMDD)"
SRC="$DATA_ROOT/$DAY"
ARCHIVE_NAME="data_${DAY}.tar.zst"
DEST_DIR="$DEST_ROOT/kalshi/$DAY"

command -v zstd >/dev/null || die "zstd is not installed"
[[ -d "$SRC" ]] || die "no data directory: $SRC"
if [[ -n "$(find "$SRC" -type f -mmin -5 -print -quit)" ]]; then
    die "a file in $SRC changed in the last 5 minutes; is the collector still writing that day?"
fi
mountpoint -q "$DEST_ROOT" || die "$DEST_ROOT is not a mount point (drive unplugged?)"
[[ -w "$DEST_ROOT" ]] || die "$DEST_ROOT is not writable"

if [[ -f "$DEST_DIR/$ARCHIVE_NAME" && -f "$DEST_DIR/$ARCHIVE_NAME.sha256" ]] \
   && (cd "$DEST_DIR" && sha256sum --check --status "$ARCHIVE_NAME.sha256"); then
    info "$DAY is already archived and verified; nothing to copy"
    run touch "$SRC/.archived"
    prune_old
    exit 0
fi

if [[ "$DRY_RUN" == 1 ]]; then info "DRY-RUN: would archive $SRC to $DEST_DIR"; exit 0; fi

# ---- compress and test locally -----------------------------------------------
TMP_DIR="$(mktemp -d "$DATA_ROOT/.archive.XXXXXX")"
ARCHIVE="$TMP_DIR/$ARCHIVE_NAME"
info "compressing $SRC with zstd -$ZSTD_LEVEL"
tar -C "$DATA_ROOT" -cf - -- "$DAY" | zstd -q -T0 "-$ZSTD_LEVEL" -o "$ARCHIVE"
zstd -q -t "$ARCHIVE"
tar --use-compress-program=unzstd -tf "$ARCHIVE" > /dev/null
( cd "$TMP_DIR" && sha256sum "$ARCHIVE_NAME" > "$ARCHIVE_NAME.sha256" )

# ---- space check on the real compressed size, with a local fallback -----------
archive_bytes="$(stat -c %s "$ARCHIVE")"
avail_bytes="$(df --output=avail -B1 -- "$DEST_ROOT" | tail -n1 | tr -d ' ')"
if (( avail_bytes < archive_bytes + 100 * 1024 * 1024 )); then
    mkdir -p -- "$FALLBACK_ROOT/$DAY"
    mv -- "$ARCHIVE" "$ARCHIVE.sha256" "$FALLBACK_ROOT/$DAY/"
    die "not enough space on $DEST_ROOT (need $archive_bytes, have $avail_bytes); archive kept in $FALLBACK_ROOT/$DAY"
fi

# ---- copy, then verify by reading back ----------------------------------------
mkdir -p -- "$DEST_DIR"
rsync -rt --partial --no-perms --no-owner --no-group --modify-window=2 \
      -- "$ARCHIVE" "$ARCHIVE.sha256" "$DEST_DIR/"
sync
( cd "$DEST_DIR" && sha256sum --check --status "$ARCHIVE_NAME.sha256" ) \
    || die "checksum mismatch after copy to $DEST_DIR; local data kept"

touch "$SRC/.archived"
info "archived and verified: $DEST_DIR/$ARCHIVE_NAME ($archive_bytes bytes)"
prune_old
```

How it works, step by step:

- **`flock -n 9`** opens a lock file on file descriptor 9 and fails immediately if another copy of the script holds it.
- **The `ERR` trap and the `EXIT` trap** split the work: the first only records the failing line, the second runs on *every* exit, cleans up the temporary directory, and sends **one** alert if the exit code is non-zero.
- **`tar ... | zstd`** streams the directory into one compressed archive without a temporary uncompressed copy. With `pipefail`, a failure in either stage fails the pipeline. If your hourly files are already `.zst` (section 9.4), compressing again gains nothing: use plain `tar -cf` or copy the directory with `rsync`.
- **The two tests** (`zstd -t` and listing the tar) catch a corrupt archive before it leaves the machine.
- **The checksum** is computed on the archive, copied alongside it, and re-checked **at the destination**. One caveat: right after a copy the destination file may still be served from the page cache, so the read-back is not a perfect disk test. For a stricter check, unmount and remount the drive (or drop caches) before verifying.
- **`.archived`** is a marker, written only after verification. `prune_old` deletes a local day only if the marker exists and the directory is older than `KEEP_DAYS`.

Test it in this order, none of which touch real data:

```bash
shellcheck archive_day.sh && bash -n archive_day.sh
DRY_RUN=1 ./archive_day.sh 20261007                 # shows what it would do
DEST_ROOT=/tmp/fake_drive ./archive_day.sh 20261007   # should refuse: not a mount point
env -i PATH=/usr/bin:/bin DATA_ROOT="$PWD/data" ./archive_day.sh 20261007   # cron-like environment
```

For a full end-to-end test, create a small loop filesystem and mount it so `mountpoint` succeeds, then corrupt the destination copy and confirm the script fails and keeps the local files (task 9.3).

### 11.4 Building blocks worth knowing

| Tool | Use |
| --- | --- |
| `flock` | Lock files; prevents overlapping runs |
| `mountpoint -q` | Is this path a mounted filesystem? |
| `mktemp -d` | Safe temporary directory |
| `find -mtime / -mmin / -print0` | Age tests; null-delimited names survive spaces |
| `df --output=avail -B1`, `du -sb`, `stat -c %s` | Free space, tree size, file size in bytes |
| `sha256sum` / `--check --status` | Checksums; `--status` returns only an exit code |
| `rsync -rt --partial ...` | Resumable copy (see 10.8 for drive flags) |
| `nice -n 19`, `ionice -c3` | Run heavy jobs without disturbing the collector |
| `shellcheck`, `shfmt`, `bash -n` | Lint, format, syntax check |
| `set -x` with `PS4='+ ${LINENO}: '` | Trace one run when debugging |

**Email delivery.** `mail` needs a working mail transfer agent on the machine, and cron has no shell profile or interactive environment, so test with the cron user's environment. A lighter alternative to configuring an MTA is `msmtp` (sends through an SMTP account you already have) or an HTTP push service called with `curl`; either replaces the `mail` call in `notify` without touching anything else.

## 12. systemd

### 12.1 Why systemd rather than cron for the collector

cron starts a command at a time and forgets it. A collector is a **long-running service**: it must start at boot, restart if it dies (but not in a tight loop), shut down cleanly on request, have its output captured, and tell you when it is unhealthy. systemd does all of that. Keep cron-style scheduling for jobs that run and finish, and for those use a systemd **timer**, which is better than cron in two ways: `Persistent=true` runs a job that was missed while the machine was off, and a failed job shows up in `systemctl` and can trigger an alert unit automatically.

### 12.2 Concepts in five lines

- A **unit** is a thing systemd manages. You will use `.service` (a process), `.timer` (a schedule that starts a service), and the built-in `.target` units (for example `network-online.target`).
- **`After=`** only controls ordering. **`Wants=`** / **`Requires=`** control whether another unit is pulled in. You usually need both: `Wants=network-online.target` plus `After=network-online.target`.
- **System units** (`/etc/systemd/system/`) run as root or as a `User=` you name, and support all sandboxing options. **User units** (`~/.config/systemd/user/`) run in your login session's manager; they need `loginctl enable-linger <user>` to run without a login, and many sandboxing options do not work for them. For an always-on collector, a **system unit with `User=`** is the simpler and sturdier choice.
- Each service runs in its own **cgroup**, so `MemoryMax=` can contain a leak and `systemctl stop` reliably kills every child process.
- stdout and stderr go to the **journal**, read with `journalctl`.

### 12.3 The collector service

First create an environment file. It is read by systemd itself (as root), so it can stay `chmod 600 root:root`:

```ini
# /etc/kalshi/collector.env
KALSHI_ACCESS_KEY=your-key-id
PRIVATE_KEY_PATH=/etc/kalshi/private_key.pem
KALSHI_DATA_DIR=/var/lib/kalshi/data
ALERT_TO=you@example.com
```

The format is **not** the same as a `.env` file for python-dotenv: no `export`, no inline `# comments` after a value, no shell expansion, and quoting rules differ. Put the private key itself in a separate file readable only by the service user (`chown lando:lando`, `chmod 600`).

Then the unit:

```ini
# /etc/systemd/system/kalshi-collector.service
[Unit]
Description=Kalshi order book collector
Wants=network-online.target
After=network-online.target
OnFailure=kalshi-notify@%n.service
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
User=lando
Group=lando
WorkingDirectory=/home/lando/kalshi-websockets
EnvironmentFile=/etc/kalshi/collector.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/lando/kalshi-websockets/.venv/bin/python kalshi_example.py --category Sports --volume 1000000

Restart=on-failure
RestartSec=10
RestartPreventExitStatus=78
KillSignal=SIGTERM
TimeoutStopSec=60

StateDirectory=kalshi
LogsDirectory=kalshi
UMask=0027
MemoryMax=2G
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

What each important line does:

- **`Restart=on-failure`** restarts after a non-zero exit, a kill by signal, or a watchdog timeout, but not after a clean exit 0. **`RestartSec=10`** waits between attempts.
- **`StartLimitIntervalSec` / `StartLimitBurst`** (these belong in `[Unit]`): if the service starts more than 10 times in 600 seconds, systemd gives up and marks it failed. That is what triggers `OnFailure=` and stops a broken service from hammering Kalshi's login endpoint.
- **`RestartPreventExitStatus=78`**: exit code 78 (`EX_CONFIG`) is a good convention for **permanent** errors such as a missing key or a 401 from the handshake (tasks 1.5, 1.6, 1.8). Restarting cannot fix those, so systemd should not retry. Transient errors (network down, 429, 5xx) should exit with a different code, or better, be retried inside the program with backoff.
- **`KillSignal=SIGTERM`** and **`TimeoutStopSec=60`**: `systemctl stop` sends SIGTERM and waits up to 60 seconds for your graceful shutdown (close the socket, drain the queue, flush, close the file; task 3.7) before sending SIGKILL. Size the timeout to your worst-case drain time.
- **`StateDirectory=kalshi`** creates `/var/lib/kalshi` owned by the service user and exports `$STATE_DIRECTORY`; `LogsDirectory=kalshi` does the same for `/var/log/kalshi`. Point `KALSHI_DATA_DIR` at the first. Your code currently writes `logs/` relative to the working directory, so add a matching `KALSHI_LOG_DIR` setting (task 1.6) or log to stderr and let the journal keep it.
- **`MemoryMax=2G`** makes a leak kill just this service instead of the machine. **`NoNewPrivileges`** and **`PrivateTmp`** are cheap hardening.

Common `ExecStart` traps: the binary path must be absolute, there is no shell (so no pipes, `&&`, or `$VAR` expansion unless you use `/bin/bash -c '...'`), and a literal `%` must be written `%%`.

### 12.4 Health: the watchdog

A process can be alive and useless (a hung event loop, a dead writer task, a deadlock). systemd's **watchdog** catches this: you set `WatchdogSec=`, and the program must send `WATCHDOG=1` more often than that interval. If it stops, systemd kills and restarts the service. It replaces the heartbeat file from task 8.7.

Add to the unit: `Type=notify`, `NotifyAccess=main`, and `WatchdogSec=120`. In Python no library is needed; the protocol is a datagram to a Unix socket:

```python
import asyncio, os, socket

def sd_notify(state: str) -> None:
    """Send e.g. 'READY=1' or 'WATCHDOG=1' to systemd. Does nothing outside systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":                 # abstract socket namespace
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.connect(addr)
        s.sendall(state.encode())

async def watchdog_loop(is_healthy) -> None:
    usec = int(os.environ.get("WATCHDOG_USEC", "0"))
    if not usec:
        return
    period = usec / 1e6 / 2           # ping at half the interval
    while True:
        if is_healthy():
            sd_notify("WATCHDOG=1")
        await asyncio.sleep(period)

# After the first successful subscribe acknowledgement: sd_notify("READY=1")
# On shutdown start:                                    sd_notify("STOPPING=1")
```

Define "healthy" as **"the event loop is running, the writer task is alive, and the writer made progress recently"**, not "frames arrived recently". Quiet overnight markets legitimately send nothing, and you do not want a restart storm at 3 a.m. In C++ link `libsystemd` and call `sd_notify(0, "READY=1")` and `sd_notify(0, "WATCHDOG=1")`.

### 12.5 Alerting on failure

`OnFailure=` starts another unit when this one enters the failed state. A **template unit** lets one alert unit serve every service:

```ini
# /etc/systemd/system/kalshi-notify@.service
[Unit]
Description=Alert: %i failed

[Service]
Type=oneshot
EnvironmentFile=/etc/kalshi/collector.env
ExecStart=/usr/local/bin/kalshi-notify.sh %i
```

```bash
#!/usr/bin/env bash
# /usr/local/bin/kalshi-notify.sh
set -Eeuo pipefail
unit="$1"
{ echo "Unit $unit failed on ${HOSTNAME:-host} at $(date)."; echo
  journalctl -u "$unit" -n 40 --no-pager; } \
  | mail -s "[${HOSTNAME:-host}] $unit failed" "${ALERT_TO:?ALERT_TO is not set}"
```

### 12.6 The nightly archive: service plus timer

```ini
# /etc/systemd/system/kalshi-archive.service
[Unit]
Description=Archive yesterday's Kalshi data to the external drive
RequiresMountsFor=/d
OnFailure=kalshi-notify@%n.service

[Service]
Type=oneshot
User=lando
WorkingDirectory=/home/lando/kalshi-websockets
EnvironmentFile=/etc/kalshi/collector.env
Environment=ALERT_TO=
Environment=DATA_ROOT=/var/lib/kalshi/data
ExecStart=/home/lando/kalshi-websockets/archive_day.sh
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=4h
```

```ini
# /etc/systemd/system/kalshi-archive.timer
[Unit]
Description=Nightly Kalshi archive

[Timer]
OnCalendar=*-*-* 01:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

Notes:

- **`Environment=ALERT_TO=`** (empty, listed after `EnvironmentFile=`) blanks the address for the script, so only `OnFailure=` sends the alert and you do not get two emails.
- **`RequiresMountsFor=/d`** makes systemd mount the drive first (if it is in `/etc/fstab`) and fail the job if it cannot. The script's own `mountpoint` check remains as a second guard.
- **`Nice=19` and `IOSchedulingClass=idle`** keep compression from competing with the collector.
- **Time zone.** The timer fires in the machine's local zone, while the script's default is "yesterday in New York". Choose the schedule in the **same zone your files are named in**. Once you switch the collector and the script to UTC (task 4.5), 01:00 UTC is a safe margin after the last hour file closes. If you stay on New York naming and the machine is in another zone, either set `TZ` consistently or put the zone in the calendar expression where your systemd version supports it.
- **`Persistent=true`** is your protection against a night when the machine was off.

### 12.7 A second line of defence: data freshness

The watchdog tells you the *program* is alive; this tells you *data is arriving*. A small script run every 15 minutes by its own timer, with `OnFailure=` wired the same way:

```bash
#!/usr/bin/env bash
# freshness_check.sh
set -Eeuo pipefail
DATA_ROOT="${DATA_ROOT:-/var/lib/kalshi/data}"
MAX_AGE_MIN="${MAX_AGE_MIN:-20}"
if [[ -z "$(find "$DATA_ROOT" -type f -mmin "-$MAX_AGE_MIN" -print -quit)" ]]; then
    echo "no data file modified in the last $MAX_AGE_MIN minutes" >&2
    exit 1
fi
```

Because the writer flushes every second, the modification time moves continuously while data flows. Choose `MAX_AGE_MIN` generously, and consider restricting the check to hours when your markets are active. A failing check alerts every 15 minutes until fixed, which is usually the right amount of annoying.

### 12.8 Everyday commands

```bash
sudo systemctl daemon-reload                       # after editing any unit file
sudo systemctl enable --now kalshi-collector       # start now and at boot
sudo systemctl enable --now kalshi-archive.timer
systemctl status kalshi-collector                  # state, last log lines, MainPID
systemctl list-timers --all                        # when each timer last ran / runs next
journalctl -u kalshi-collector -f -o cat           # follow the log
journalctl -u kalshi-collector --since "1 hour ago" -p warning
sudo systemctl start kalshi-archive.service        # run the archive now, by hand
systemd-analyze verify /etc/systemd/system/kalshi-*.service
systemd-analyze security kalshi-collector          # sandboxing score, with suggestions
sudo systemctl edit kalshi-collector               # override settings without editing the file
```

Before you trust it, test the failure behaviour deliberately: `kill -9` the main process (it should restart in about 10 seconds), `systemctl stop` it (it should finish within the stop timeout and exit 0), pull the network cable (it should reconnect with backoff), and start the archive service with the drive unmounted (it should fail with exactly one alert). The collector must not draw its terminal display when there is no terminal, or the journal fills with escape codes (task 8.3); the check is `sys.stdout.isatty()`.

For the journal itself, set `Storage=persistent` and `SystemMaxUse=500M` in `/etc/systemd/journald.conf` so logs survive reboots without growing forever.

## 13. Putting it together

### 13.1 Suggested order of work

This follows your `TASKS.md` order, with the new material slotted in where it pays off most:

1. **Finish crash safety** (tasks 1.2 to 1.6). Make background tasks supervised, handshake errors distinguishable, startup validation clean, and paths absolute. Add the exit-code convention (78 for permanent errors).
2. **Reconnect and shutdown** (Phase 3): backoff, connect and disconnect markers, `seq` gap detection per `sid`, graceful SIGTERM.
3. **Logging correctness** (tasks 4.1, 4.2): counters and the received-equals-written invariant, one-second flush. Decide the line format (section 9.4) *now*, before you have a month of data in the old one.
4. **Compress while writing** and run the benchmark from section 9.2 on a real hour.
5. **Replace `copy_files.sh`** with `archive_day.sh` (section 11.3), then test it against a loop-mounted filesystem.
6. **Install the systemd units** (section 12), including the watchdog helper and the failure-alert template.
7. **Daily extras:** subscription list and market metadata snapshots (section 10.5), the manifest (10.4), and the Parquet conversion (9.5).
8. **Run the 24-hour soak test** from `TASKS.md`, with one deliberate outage, and read the morning quality report.
9. **Only then start the C++ port** (section 8), using the Python collector's recordings as the ground truth to test it against.

### 13.2 Things I noticed that are not in `TASKS.md`

| Finding | Where | Fix |
| --- | --- | --- |
| A clean server close sleeps 6 seconds, not 3 (a `sleep(3)` inside the `try`, another after the `except` block) | `orderbook_websocket` | Remove the first sleep |
| `fake_server.py` modes `error`, `seqgap`, `badfield` do not do what they say; fault triggers re-roll per frame | `tests/fake_server.py` | See section 3.4 |
| `seq` is per subscription (`sid`), not per market | Fixture: seq 1 to 6 are six snapshots of six markets | Track one counter per `sid` |
| Raw frames end with a newline | Fixture (`\n` inside `msg`) | Strip it if you change the line format |
| Bursts of dozens of deltas inside one millisecond produce intermediate OFI states | Fixture, around seq 409 to 560 | Group by `ts_ms` (section 1.4) |
| `.gitignore` hides `CMakeLists.txt`, `*.hpp`, `vcpkg.json`, and `pyproject.toml` | `.gitignore` | Re-include lines (section 8.4) |
| `.gitattributes` does not force LF for shell scripts and unit files | `.gitattributes` | Add `*.sh`, `*.service`, `*.timer` (section 8.4) |
| `except InvalidSignature` around `sign()` can never fire | `connection_utils.py` | Delete it |
| The scheduled job time and file naming use different time zones | `copy_files.sh`, `log_path` | Use UTC everywhere |
| `StateDirectory=` and `LogsDirectory=` are needed if you sandbox the service | Future systemd unit | Make data and log paths configurable |

### 13.3 Reference material

- **Standards:** RFC 6455 (WebSocket protocol), RFC 8017 (RSA-PSS).
- **Man pages:** `systemd.service(5)`, `systemd.exec(5)`, `systemd.timer(5)`, `systemd.time(7)`, `sd_notify(3)`, `rsync(1)`, `zstd(1)`, `flock(1)`.
- **Exchange:** Kalshi's API documentation, for the authoritative field list, WebSocket limits (markets per subscription, connections per account; task 3.9), and rate limits.
- **Research:** Cont, Kukanov and Stoikov (2014), *The Price Impact of Order Book Events*, which defines the OFI you implemented.
- **Libraries:** the `websockets`, `cryptography`, `requests`, and DuckDB documentation; Boost.Beast, OpenSSL (`EVP_PKEY_CTX_set_rsa_pss_saltlen` and friends), libcurl, simdjson, and libzstd for the C++ side.
