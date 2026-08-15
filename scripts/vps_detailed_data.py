import httpx, json, time, asyncio, websockets, sys
from datetime import datetime, timezone

now_ts = int(time.time())
window_ts = (now_ts // 300) * 300
slug = f"btc-updown-5m-{window_ts}"
close_ts = window_ts + 300

print(f"Detailed Data Capture - {datetime.now(timezone.utc).isoformat()}")
print(f"Window: {window_ts}, Slug: {slug}, Close in {close_ts - now_ts}s")
print()

# Gamma full response
r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=15)
m = r.json()[0]
tokens_raw = m.get("clobTokenIds", "")
outcomes_raw = m.get("outcomes", "")
if isinstance(tokens_raw, str) and tokens_raw.startswith("["):
    token_ids = json.loads(tokens_raw)
elif isinstance(tokens_raw, list):
    token_ids = tokens_raw
else:
    token_ids = []
if isinstance(outcomes_raw, str) and outcomes_raw.startswith("["):
    outcomes = json.loads(outcomes_raw)
elif isinstance(outcomes_raw, list):
    outcomes = outcomes_raw
else:
    outcomes = []

up_idx = outcomes.index("Up") if "Up" in outcomes else 0
down_idx = outcomes.index("Down") if "Down" in outcomes else 1

print("=== MARKET METADATA ===")
print(f"Slug: {slug}")
print(f"Condition ID: {m.get('conditionId', '')}")
print(f"Active: {m.get('active')}")
print(f"Closed: {m.get('closed')}")
print(f"End Date: {m.get('endDateIso', m.get('endDate', ''))}")
print(f"Outcomes: {outcomes}")
print(f"UP Token:   {token_ids[up_idx] if len(token_ids) > up_idx else '?'}")
print(f"DOWN Token: {token_ids[down_idx] if len(token_ids) > down_idx else '?'}")

events = m.get("events", [])
if events:
    em = events[0].get("eventMetadata", {})
    print(f"PTB: {em.get('priceToBeat', 'N/A')}")
    print(f"Final Price: {em.get('finalPrice', 'N/A')}")
    print(f"Outcome: {em.get('outcome', 'N/A')}")
print()

# CLOB orderbook for both UP and DOWN
for label, idx in [("UP", up_idx), ("DOWN", down_idx)]:
    tid = token_ids[idx] if len(token_ids) > idx else ""
    if not tid:
        continue
    r2 = httpx.get("https://clob.polymarket.com/book", params={"token_id": tid}, timeout=15)
    book = r2.json()
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    best_bid = bids[0]["price"] if bids else "none"
    best_ask = asks[0]["price"] if asks else "none"
    bid_depth = sum(float(b.get("size", 0)) for b in bids)
    ask_depth = sum(float(a.get("size", 0)) for a in asks)
    print(f"=== {label} ORDERBOOK ===")
    print(f"Token: {tid}")
    print(f"Best Bid: {best_bid} (depth: {bid_depth:.2f})")
    print(f"Best Ask: {best_ask} (depth: {ask_depth:.2f})")
    print(f"Bid levels: {len(bids)}, Ask levels: {len(asks)}")
    print()

# Settlement data (previous market)
prev_slug = f"btc-updown-5m-{window_ts - 300}"
r3 = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": prev_slug}, timeout=15)
if r3.status_code == 200 and r3.json():
    pm = r3.json()[0]
    print(f"=== PREVIOUS MARKET SETTLEMENT ===")
    print(f"Slug: {prev_slug}")
    print(f"Closed: {pm.get('closed')}")
    pevents = pm.get("events", [])
    if pevents:
        pem = pevents[0].get("eventMetadata", {})
        print(f"Outcome: {pem.get('outcome', 'N/A')}")
        print(f"Final Price: {pem.get('finalPrice', 'N/A')}")
        print(f"PTB: {pem.get('priceToBeat', 'N/A')}")
print()

# RTDS sample messages
async def sample_rtds():
    url = "wss://ws-live-data.polymarket.com"
    async with websockets.connect(url, open_timeout=10) as ws:
        sub = {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "update"}]}
        await ws.send(json.dumps(sub))
        msgs = []
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and len(msgs) < 3:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3)
                if raw != "PING":
                    msgs.append(raw)
            except asyncio.TimeoutError:
                break
        print(f"=== RTDS SAMPLE MESSAGES ({len(msgs)} received) ===")
        for i, msg in enumerate(msgs):
            try:
                parsed = json.loads(msg)
                print(f"Message {i+1}: {json.dumps(parsed, indent=2)[:400]}")
            except json.JSONDecodeError:
                print(f"Message {i+1} (raw): {msg[:400]}")

asyncio.run(sample_rtds())
