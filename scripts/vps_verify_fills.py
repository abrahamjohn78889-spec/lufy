"""Verify post-fill DB state: orders, fills, consistency."""
import paramiko
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

def run(cmd):
    _, out, err = ssh.exec_command(cmd)
    return out.read().decode("utf-8", errors="replace"), err.read().decode("utf-8", errors="replace")

# Orders
out, _ = run(f"cd {REMOTE} && python3 -c 'import sqlite3,json;c=sqlite3.connect(\"data/arc.db\");c.row_factory=sqlite3.Row;rows=c.execute(\"SELECT order_id,state,direction,price,size,filled_size FROM orders ORDER BY created_at DESC LIMIT 20\").fetchall();print(json.dumps([dict(r) for r in rows],indent=2))'")
print("=== ORDERS (recent) ===")
print(out[:3000])

# Fills
out, _ = run(f"cd {REMOTE} && python3 -c 'import sqlite3,json;c=sqlite3.connect(\"data/arc.db\");c.row_factory=sqlite3.Row;rows=c.execute(\"SELECT fill_id,order_id,market_slug,size,price,engine FROM fills ORDER BY rowid DESC LIMIT 20\").fetchall();print(json.dumps([dict(r) for r in rows],indent=2))'")
print("\n=== FILLS (recent) ===")
print(out[:3000])

# Settlements
out, _ = run(f"cd {REMOTE} && python3 -c 'import sqlite3,json;c=sqlite3.connect(\"data/arc.db\");c.row_factory=sqlite3.Row;rows=c.execute(\"SELECT * FROM settlements ORDER BY rowid DESC LIMIT 10\").fetchall();print(json.dumps([dict(r) for r in rows],indent=2))'")
print("\n=== SETTLEMENTS (recent) ===")
print(out[:2000])

# Ledger
out, _ = run(f"cd {REMOTE} && python3 -c 'import sqlite3,json;c=sqlite3.connect(\"data/arc.db\");c.row_factory=sqlite3.Row;rows=c.execute(\"SELECT * FROM ledger ORDER BY rowid DESC LIMIT 10\").fetchall();print(json.dumps([dict(r) for r in rows],indent=2))'")
print("\n=== LEDGER (recent) ===")
print(out[:2000])

# Markets current
out, _ = run(f"cd {REMOTE} && python3 -c 'import sqlite3,json;c=sqlite3.connect(\"data/arc.db\");c.row_factory=sqlite3.Row;rows=c.execute(\"SELECT slug,phase,close_ts,settled_outcome FROM markets WHERE phase IN (\\\"SETTLING\\\",\\\"SETTLED\\\") ORDER BY close_ts DESC LIMIT 5\").fetchall();print(json.dumps([dict(r) for r in rows],indent=2))'")
print("\n=== MARKETS (settling/settled) ===")
print(out[:2000])

ssh.close()
