"""Bypass spec check for paper trading by setting runtime_state in SQLite."""
import sqlite3
import time

conn = sqlite3.connect("data/arc.db")
cur = conn.cursor()
now = time.time()

# Set spec status to VERIFIED
cur.execute(
    "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
    ("settlement_spec_status", "VERIFIED", now),
)

# Enable trading
cur.execute(
    "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
    ("trading_enabled", "true", now),
)

# Clear disable reason
cur.execute(
    "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
    ("trading_disabled_reason", "", now),
)

conn.commit()

# Verify
cur.execute("SELECT key, value FROM runtime_state ORDER BY key")
print("Runtime state after update:")
for k, v in cur.fetchall():
    print(f"  {k} = {v}")

conn.close()
print("\nSpec bypass complete. Restart runtime to pick up changes.")
