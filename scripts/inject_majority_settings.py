"""Inject MAJORITY settings into the ARC SQLite settings table on the VPS."""
import sqlite3
import sys

db_path = sys.argv[1] if len(sys.argv) > 1 else "data/arc.db"

# Baseline config: MAJORITY ON, BUFFER OFF, TRIGGER+TARGET OFF, PRICE RETRY OFF
majority_settings = {
    "majority_enabled": "true",
    "majority_trigger_limit_enabled": "false",
    "majority_buffer_enabled": "false",
    "majority_price_retry_enabled": "false",
    "majority_price_retry_attempts": "5",
    "majority_execution_windows": "15,25,35,45,120,150",
    # Required values even when buffer/trigger OFF (validated at config time)
    "majority_buffer": "0.00",
    "majority_trigger_price": "0.60",
    "majority_target_limit_price": "0.60",
    "majority_shares": "5",
    "majority_entry_price_min": "0.55",
    "majority_entry_price_max": "0.85",
}

conn = sqlite3.connect(db_path)
cur = conn.cursor()

import time
now = time.time()

for key, value in majority_settings.items():
    cur.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, now),
    )

conn.commit()

# Verify
cur.execute("SELECT key, value FROM settings WHERE key LIKE 'majority%' ORDER BY key")
rows = cur.fetchall()
print(f"MAJORITY settings written ({len(rows)} rows):")
for k, v in rows:
    print(f"  {k} = {v}")

conn.close()
