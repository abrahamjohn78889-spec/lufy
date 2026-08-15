import httpx, json, time, asyncio, websockets, math, sys
from datetime import datetime, timezone

results = []

def record(name, endpoint, status, latency_ms, data_summary="", failure=""):
    ts = datetime.now(timezone.utc).isoformat()
    results.append({
        "name": name,
        "endpoint": endpoint,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "timestamp": ts,
        "data": data_summary[:500],
        "failure": failure
    })
    icon = "PASS" if status == "PASS" else "FAIL"
    print(f"[{icon}] {name}: {latency_ms:.0f}ms" + (f" -- {failure}" if failure else ""))

now_ts = int(time.time())
window_ts = (now_ts // 300) * 300
slug = f"btc-updown-5m-{window_ts}"
close_ts = window_ts + 300

print(f"=== PART 1 CONNECTIVITY TESTS ===")
print(f"Now: {now_ts}, Window: {window_ts}, Slug: {slug}")
print(f"Close: {close_ts} ({close_ts - now_ts}s remaining)")
print()

# 1. Gamma API
try:
    t0 = time.monotonic()
    r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=15)
    lat = (time.monotonic() - t0) * 1000
    if r.status_code == 200 and r.json():
        m = r.json()[0]
        tokens = m.get("clobTokenIds", "")
        outcomes = m.get("outcomes", "")
        active = m.get("active", False)
        closed = m.get("closed", False)
        events = m.get("events", [])
        ptb_raw = ""
        if events:
            em = events[0].get("eventMetadata", {})
            ptb_raw = em.get("priceToBeat", "")
        summary = f"tokens={tokens[:80]} outcomes={outcomes} active={active} closed={closed} ptb={ptb_raw}"
        record("Gamma API", f"gamma-api.polymarket.com/markets?slug={slug}", "PASS", lat, summary)
    else:
        record("Gamma API", f"gamma-api.polymarket.com/markets?slug={slug}", "FAIL", lat, "", f"HTTP {r.status_code}, body={r.text[:200]}")
except Exception as e:
    record("Gamma API", "gamma-api.polymarket.com", "FAIL", 0, "", str(e)[:200])

# 2. CLOB API (orderbook for first token)
try:
    r2 = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=15)
    market_data = r2.json()[0] if r2.status_code == 200 and r2.json() else {}
    token_ids_str = market_data.get("clobTokenIds", "")
    if isinstance(token_ids_str, str) and token_ids_str.startswith("["):
        token_ids = json.loads(token_ids_str)
    elif isinstance(token_ids_str, list):
        token_ids = token_ids_str
    else:
        token_ids = [token_ids_str] if token_ids_str else []

    if token_ids:
        up_token = token_ids[0]
        t0 = time.monotonic()
        r = httpx.get("https://clob.polymarket.com/book", params={"token_id": up_token}, timeout=15)
        lat = (time.monotonic() - t0) * 1000
        if r.status_code == 200:
            book = r.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = bids[0]["price"] if bids else "none"
            best_ask = asks[0]["price"] if asks else "none"
            summary = f"UP token={up_token[:20]}... bids={len(bids)} asks={len(asks)} best_bid={best_bid} best_ask={best_ask}"
            record("CLOB Orderbook", "clob.polymarket.com/book", "PASS", lat, summary)
        else:
            record("CLOB Orderbook", "clob.polymarket.com/book", "FAIL", lat, "", f"HTTP {r.status_code}")
    else:
        record("CLOB Orderbook", "clob.polymarket.com/book", "FAIL", 0, "", "No token IDs from Gamma")
except Exception as e:
    record("CLOB Orderbook", "clob.polymarket.com", "FAIL", 0, "", str(e)[:200])

# 3. Market Discovery (full metadata parse)
try:
    t0 = time.monotonic()
    r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=15)
    lat = (time.monotonic() - t0) * 1000
    if r.status_code == 200 and r.json():
        m = r.json()[0]
        condition_id = m.get("conditionId", "")
        tokens_raw = m.get("clobTokenIds", "")
        outcomes_raw = m.get("outcomes", "")
        venue_close = m.get("endDateIso", "") or m.get("endDate", "")
        events = m.get("events", [])
        ptb = ""
        final_price = ""
        if events:
            em = events[0].get("eventMetadata", {})
            ptb = em.get("priceToBeat", "")
            final_price = em.get("finalPrice", "")

        if isinstance(tokens_raw, str) and tokens_raw.startswith("["):
            tokens = json.loads(tokens_raw)
        elif isinstance(tokens_raw, list):
            tokens = tokens_raw
        else:
            tokens = []
        if isinstance(outcomes_raw, str) and outcomes_raw.startswith("["):
            outcomes = json.loads(outcomes_raw)
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = []

        up_idx = outcomes.index("Up") if "Up" in outcomes else 0
        down_idx = outcomes.index("Down") if "Down" in outcomes else 1
        up_token = tokens[up_idx] if len(tokens) > up_idx else "?"
        down_token = tokens[down_idx] if len(tokens) > down_idx else "?"

        summary = f"condition={condition_id[:20]}... UP={up_token[:20]}... DOWN={down_token[:20]}... close={venue_close} ptb={ptb} final={final_price}"
        record("Market Discovery", f"gamma-api/markets?slug={slug}", "PASS", lat, summary)
    else:
        record("Market Discovery", "gamma-api/markets", "FAIL", lat, "", f"No market found for {slug}")
except Exception as e:
    record("Market Discovery", "gamma-api/markets", "FAIL", 0, "", str(e)[:200])

# 4. BTC Price Feed
try:
    t0 = time.monotonic()
    r = httpx.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=10)
    lat = (time.monotonic() - t0) * 1000
    if r.status_code == 200:
        price = r.json().get("price", "?")
        record("BTC Price (Binance ref)", "api.binance.com/api/v3/ticker/price", "PASS", lat, f"BTCUSDT={price}")
    else:
        record("BTC Price (Binance ref)", "api.binance.com", "FAIL", lat, "", f"HTTP {r.status_code}")
except Exception as e:
    record("BTC Price (Binance ref)", "api.binance.com", "FAIL", 0, "", str(e)[:200])

# 5. RTDS WebSocket
async def test_rtds():
    url = "wss://ws-live-data.polymarket.com"
    try:
        t0 = time.monotonic()
        async with websockets.connect(url, open_timeout=10) as ws:
            handshake_lat = (time.monotonic() - t0) * 1000
            sub_frame = {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update"}]}
            await ws.send(json.dumps(sub_frame))
            msgs = []
            keepalives = 0
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and len(msgs) < 5:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    if raw == "PING":
                        await ws.send("PING")
                        keepalives += 1
                    else:
                        msgs.append(raw[:200])
                except asyncio.TimeoutError:
                    break
            total_lat = (time.monotonic() - t0) * 1000
            if msgs:
                sample = msgs[0][:150]
                record("RTDS WebSocket", url, "PASS", total_lat,
                       f"handshake={handshake_lat:.0f}ms msgs={len(msgs)} keepalives={keepalives} sample={sample}")
            elif keepalives > 0:
                record("RTDS WebSocket", url, "PASS", total_lat,
                       f"handshake={handshake_lat:.0f}ms keepalives={keepalives} msgs=0 (keepalive-only)")
            else:
                record("RTDS WebSocket", url, "FAIL", total_lat, "", "No messages or keepalives received")
    except Exception as e:
        record("RTDS WebSocket", url, "FAIL", 0, "", str(e)[:200])

asyncio.run(test_rtds())

# 6. Next market prefetch
try:
    next_window_ts = window_ts + 300
    next_slug = f"btc-updown-5m-{next_window_ts}"
    t0 = time.monotonic()
    r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": next_slug}, timeout=15)
    lat = (time.monotonic() - t0) * 1000
    if r.status_code == 200 and r.json():
        record("Next Market Prefetch", f"gamma-api/markets?slug={next_slug}", "PASS", lat, f"next_slug={next_slug}")
    else:
        record("Next Market Prefetch", f"gamma-api/markets?slug={next_slug}", "FAIL", lat, "", "Not yet available (expected near boundary)")
except Exception as e:
    record("Next Market Prefetch", "gamma-api/markets", "FAIL", 0, "", str(e)[:200])

# 7. Settlement/outcome check (previous market)
try:
    prev_window_ts = window_ts - 300
    prev_slug = f"btc-updown-5m-{prev_window_ts}"
    t0 = time.monotonic()
    r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": prev_slug}, timeout=15)
    lat = (time.monotonic() - t0) * 1000
    if r.status_code == 200 and r.json():
        m = r.json()[0]
        closed = m.get("closed", False)
        events = m.get("events", [])
        outcome = ""
        final = ""
        if events:
            em = events[0].get("eventMetadata", {})
            outcome = em.get("outcome", "")
            final = em.get("finalPrice", "")
        summary = f"prev={prev_slug} closed={closed} outcome={outcome} final={final}"
        record("Settlement Data", f"gamma-api/markets?slug={prev_slug}", "PASS", lat, summary)
    else:
        record("Settlement Data", f"gamma-api/markets?slug={prev_slug}", "FAIL", lat, "", "Not found")
except Exception as e:
    record("Settlement Data", "gamma-api/markets", "FAIL", 0, "", str(e)[:200])

print()
print("=== SUMMARY ===")
passed = sum(1 for r in results if r["status"] == "PASS")
failed = sum(1 for r in results if r["status"] == "FAIL")
print(f"PASS: {passed}/{len(results)}, FAIL: {failed}/{len(results)}")
for r in results:
    flag = "PASS" if r["status"] == "PASS" else "FAIL"
    print(f"  [{flag}] {r['name']} ({r['latency_ms']:.0f}ms)" + (f" - {r['failure']}" if r['failure'] else ""))
