"""Check current market state and rotation."""
import httpx
import json
import sqlite3
import time

now_ts = int(time.time())
window_ts = (now_ts // 300) * 300
current_slug = f"btc-updown-5m-{window_ts}"
next_slug = f"btc-updown-5m-{window_ts + 300}"

print(f"Current time: {now_ts}")
print(f"Current window: {window_ts}")
print(f"Current slug: {current_slug}")
print(f"Next slug: {next_slug}")
print(f"Seconds until close: {window_ts + 300 - now_ts}")

# Check Gamma for current market
r = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": current_slug}, timeout=10)
if r.status_code == 200 and r.json():
    m = r.json()[0]
    print(f"\n=== CURRENT MARKET ({current_slug}) ===")
    print(f"  Active: {m.get('active')}")
    print(f"  Closed: {m.get('closed')}")
    tokens = m.get("clobTokenIds", "")
    outcomes = m.get("outcomes", "")
    print(f"  Tokens: {tokens[:100]}...")
    print(f"  Outcomes: {outcomes}")
else:
    print(f"\nCurrent market not found on Gamma")

# Check DB markets
conn = sqlite3.connect("data/arc.db")
cur = conn.cursor()

# Get all markets in DB
cur.execute("SELECT * FROM markets ORDER BY rowid DESC LIMIT 5")
cols = [d[0] for d in cur.description]
rows = cur.fetchall()
print(f"\n=== MARKETS IN DB (latest 5) ===")
for row in rows:
    d = dict(zip(cols, row))
    print(f"  {d}")

# Check latest observations
cur.execute("SELECT market_slug, MAX(ts), COUNT(*) FROM observations GROUP BY market_slug ORDER BY MAX(ts) DESC LIMIT 5")
obs_summary = cur.fetchall()
print(f"\n=== OBSERVATIONS BY MARKET ===")
for slug, max_ts, count in obs_summary:
    age = now_ts - int(max_ts) if max_ts else None
    print(f"  {slug}: {count} obs, last ts={max_ts}, age={age}s")

# Check intents
cur.execute("SELECT COUNT(*) FROM intents")
intent_count = cur.fetchone()[0]
print(f"\nTotal intents: {intent_count}")

conn.close()
