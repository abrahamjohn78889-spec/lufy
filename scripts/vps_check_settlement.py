import paramiko, sys
sys.stdout.reconfigure(encoding="utf-8")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)

_, o, e = ssh.exec_command("""cd /home/roots/arc_project && python3 << 'PYEOF'
import sqlite3, json
c = sqlite3.connect("data/arc.db")
c.row_factory = sqlite3.Row
# Schema
rows = c.execute("PRAGMA table_info(markets)").fetchall()
print("MARKETS_SCHEMA:", [r["name"] for r in rows])
rows = c.execute("SELECT * FROM markets ORDER BY rowid DESC LIMIT 5").fetchall()
print("MARKETS:", json.dumps([dict(r) for r in rows], indent=2))
rows = c.execute("PRAGMA table_info(settlements)").fetchall()
print("SETTLEMENTS_SCHEMA:", [r["name"] for r in rows])
rows = c.execute("SELECT * FROM settlements ORDER BY rowid DESC LIMIT 10").fetchall()
print("SETTLEMENTS:", json.dumps([dict(r) for r in rows], indent=2))
rows = c.execute("SELECT order_id, state, direction, price, size, filled_size FROM orders WHERE market_slug='btc-updown-5m-1786791000'").fetchall()
print("ORDERS_1786791000:", json.dumps([dict(r) for r in rows], indent=2))
rows = c.execute("SELECT fill_id, order_id, size, price FROM fills WHERE market_slug='btc-updown-5m-1786791000'").fetchall()
print("FILLS_1786791000:", json.dumps([dict(r) for r in rows], indent=2))
PYEOF""")
out = o.read().decode("utf-8", errors="replace")
err = e.read().decode("utf-8", errors="replace")
print(out)
if err:
    print("ERR:", err[:500])
ssh.close()
