"""Check feed health and observation counts."""
import httpx
import json
import sqlite3

# Check API status details
base = "http://127.0.0.1:9080"
r = httpx.get(f"{base}/status", timeout=5)
d = r.json()
rt = d.get("runtime", {})

print("=== FEED METRICS ===")
for k in ["feed_age_ms", "clock_drift_ms", "health_revision"]:
    print(f"  {k}: {rt.get(k)}")

# Check observations in DB
conn = sqlite3.connect("data/arc.db")
cur = conn.cursor()

# Recent observations count
cur.execute("SELECT COUNT(*) FROM observations WHERE received_at > ?",
            (rt.get("started_at", 0),))
count = cur.fetchone()[0]
print(f"\nObservations since runtime start: {count}")

# Latest observation
cur.execute("SELECT market_slug, ts, price, window_seconds, received_at FROM observations ORDER BY id DESC LIMIT 3")
rows = cur.fetchall()
print("\nLatest observations:")
for row in rows:
    try:
        price = float(row[2]) if row[2] else 0
        print(f"  slug={row[0]} ts={row[1]} price={price:.2f} ws={row[3]} recv={row[4]}")
    except:
        print(f"  slug={row[0]} ts={row[1]} price={row[2]} ws={row[3]} recv={row[4]}")

# Check markets table
cur.execute("SELECT slug, active, close_ts FROM markets ORDER BY close_ts DESC LIMIT 5")
markets = cur.fetchall()
print("\nRecent markets:")
for m in markets:
    print(f"  {m[0]} active={m[1]} close={m[2]}")

# Check intents (trades)
cur.execute("SELECT COUNT(*) FROM intents")
intent_count = cur.fetchone()[0]
print(f"\nTotal intents (trades): {intent_count}")

if intent_count > 0:
    cur.execute("SELECT * FROM intents ORDER BY created_at DESC LIMIT 5")
    cols = [d[0] for d in cur.description]
    intents = cur.fetchall()
    print("\nRecent intents:")
    for i, row in enumerate(intents):
        print(f"  Intent {i+1}:")
        for c, v in zip(cols, row):
            print(f"    {c} = {v}")

conn.close()
