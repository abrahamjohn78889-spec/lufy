"""Check orders table state and why fills aren't happening."""
import paramiko
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

diag = """
import sqlite3, json
conn = sqlite3.connect('data/arc.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== ORDERS (all 6) ===")
cur.execute("SELECT * FROM orders ORDER BY created_at")
rows = [dict(r) for r in cur.fetchall()]
for r in rows:
    print(json.dumps(r, indent=2))

print("\\n=== WINDOWS (last 20) ===")
cur.execute("SELECT * FROM windows ORDER BY rowid DESC LIMIT 20")
rows = [dict(r) for r in cur.fetchall()]
for r in rows:
    print(r)

print("\\n=== RUNTIME SESSIONS ===")
cur.execute("SELECT * FROM runtime_sessions ORDER BY rowid DESC LIMIT 2")
rows = [dict(r) for r in cur.fetchall()]
for r in rows:
    print(json.dumps(r, indent=2))

conn.close()
"""

stdin, stdout, stderr = ssh.exec_command(f"cat > {REMOTE_BASE}/check_orders.py << 'PYEOF'\n{diag}\nPYEOF")
stdout.read()
stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 check_orders.py")
print(stdout.read().decode('utf-8', errors='replace'))

# Check for fill-related errors in log
stdin, stdout, stderr = ssh.exec_command(
    f'grep -i "fill\\|error\\|exception\\|traceback\\|failed" {REMOTE_BASE}/logs/runtime.log | grep -v "Book Unavailable" | tail -30'
)
print(f"\n=== ERRORS/FILLS ===\n{stdout.read().decode('utf-8', errors='replace')}")

ssh.close()
