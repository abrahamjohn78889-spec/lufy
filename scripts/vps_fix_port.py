"""Fix api_port in SQLite settings on VPS."""
import sqlite3
import time

db_path = "data/arc.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()
now = time.time()

cur.execute(
    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
    ("api_port", "9080", now),
)
conn.commit()

# Verify
cur.execute("SELECT key, value FROM settings WHERE key IN ('api_port', 'api_bind')")
for k, v in cur.fetchall():
    print(f"{k} = {v}")

conn.close()
print("PORT FIXED TO 9080")
