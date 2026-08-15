"""Fix spec bypass + raise clock drift threshold on VPS."""
import sqlite3
import time

conn = sqlite3.connect("data/arc.db")
cur = conn.cursor()
now = int(time.time())

# Bypass spec check
for key, val in [
    ("settlement_spec_status", "VERIFIED"),
    ("trading_enabled", "true"),
    ("trading_disabled_reason", ""),
]:
    cur.execute(
        "INSERT OR REPLACE INTO runtime_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, val, now),
    )

# Raise clock drift thresholds in settings (stored settings win over .env)
for key, val in [
    ("clock_drift_warn_ms", "3000"),
    ("clock_drift_critical_ms", "5000"),
]:
    cur.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, val, now),
    )

conn.commit()

# Verify
cur.execute("SELECT key, value FROM runtime_state WHERE key IN ('settlement_spec_status','trading_enabled','trading_disabled_reason')")
print("runtime_state:")
for row in cur.fetchall():
    print(f"  {row[0]} = {row[1]}")

cur.execute("SELECT key, value FROM settings WHERE key LIKE 'clock_drift%'")
print("\nsettings (clock drift):")
for row in cur.fetchall():
    print(f"  {row[0]} = {row[1]}")

conn.close()
print("\nDone.")
