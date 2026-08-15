"""Check PTB availability for current market."""
import httpx
import json
import time

now_ts = int(time.time())
window_ts = (now_ts // 300) * 300

for offset in [0, -300, -600]:
    wts = window_ts + offset
    slug = f"btc-updown-5m-{wts}"

    r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=10)
    if r.status_code == 200 and r.json():
        m = r.json()[0]
        events = m.get("events", [])
        ptb = None
        final = None
        outcome = None
        if events:
            em = events[0].get("eventMetadata", {})
            ptb = em.get("priceToBeat")
            final = em.get("finalPrice")
            outcome = em.get("outcome")

        print(f"\n=== {slug} ===")
        print(f"  Active: {m.get('active')}, Closed: {m.get('closed')}")
        print(f"  PTB: {ptb}")
        print(f"  FinalPrice: {final}")
        print(f"  Outcome: {outcome}")
        print(f"  Events count: {len(events)}")
        if events:
            print(f"  eventMetadata keys: {list(events[0].get('eventMetadata', {}).keys())}")
            print(f"  Full eventMetadata: {json.dumps(events[0].get('eventMetadata', {}), indent=4)[:500]}")
    else:
        print(f"\n{slug}: NOT FOUND")
