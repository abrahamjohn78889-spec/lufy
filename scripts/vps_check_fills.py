"""Check all DB tables, paper executor logs, and fill status."""
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

# All tables + schemas
diag = """
import sqlite3, json
conn = sqlite3.connect('data/arc.db')
cur = conn.cursor()

print("=== ALL TABLES ===")
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cur.fetchall()]
print(tables)

for t in tables:
    print(f"\\n=== {t} SCHEMA ===")
    cur.execute(f"PRAGMA table_info({t})")
    cols = [(r[1], r[2]) for r in cur.fetchall()]
    print(cols)
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"rows: {cur.fetchone()[0]}")

conn.close()
"""

stdin, stdout, stderr = ssh.exec_command(f"cat > {REMOTE_BASE}/check_schema.py << 'PYEOF'\n{diag}\nPYEOF")
stdout.read()
stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 check_schema.py")
print(stdout.read().decode('utf-8', errors='replace'))

# Check for fill/execution/paper logs
stdin, stdout, stderr = ssh.exec_command(
    f'grep -i "fill\\|paper\\|execute\\|order\\|ledger\\|v1\\|submitted" {REMOTE_BASE}/logs/runtime.log | tail -30'
)
print(f"\n=== EXECUTION LOGS ===\n{stdout.read().decode('utf-8', errors='replace')}")

# Check history endpoint
stdin, stdout, stderr = ssh.exec_command('curl -s "http://127.0.0.1:9080/history"')
raw = stdout.read().decode()
try:
    d = json.loads(raw)
    print(f"\n=== HISTORY ===\n{json.dumps(d, indent=2)[:2000]}")
except Exception as e:
    print(f"\nHistory error: {e}, raw={raw[:500]}")

ssh.close()
