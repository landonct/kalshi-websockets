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
