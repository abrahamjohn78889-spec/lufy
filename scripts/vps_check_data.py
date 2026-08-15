"""Check RTDS message structure and observation data on VPS."""
import sqlite3
import json

conn = sqlite3.connect("data/arc.db")
cur = conn.cursor()

# Check observations table structure
cur.execute("PRAGMA table_info(observations)")
cols = [r[1] for r in cur.fetchall()]
print("Observations columns:", cols)

# Get sample observations
cur.execute("SELECT * FROM observations LIMIT 3")
rows = cur.fetchall()
for i, row in enumerate(rows):
    d = dict(zip(cols, row))
    print(f"\nObservation {i+1}:")
    for k, v in d.items():
        print(f"  {k} = {v}")

# Check runtime_state
print("\n--- Runtime State ---")
cur.execute("SELECT key, value FROM runtime_state")
for k, v in cur.fetchall():
    print(f"  {k} = {v}")

# Check if there's a raw payload stored anywhere
tables = []
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
for (name,) in cur.fetchall():
    tables.append(name)
print(f"\nTables: {tables}")

conn.close()
