"""Quick check: are the 2 live orders still SUBMITTED or have they been filled/cancelled?"""
import paramiko
import json
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

# Check current order states
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3,json; conn = sqlite3.connect(\\"data/arc.db\\"); conn.row_factory = sqlite3.Row; cur = conn.cursor(); '
    'cur.execute(\\"SELECT order_id, state, filled_size, rejection_reason FROM orders ORDER BY created_at DESC LIMIT 10\\"); '
    'rows = [dict(r) for r in cur.fetchall()]; print(json.dumps(rows, indent=2)); conn.close()"'
)
print("=== CURRENT ORDER STATES ===")
print(stdout.read().decode('utf-8', errors='replace'))

# Check fills again
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3,json; conn = sqlite3.connect(\\"data/arc.db\\"); conn.row_factory = sqlite3.Row; cur = conn.cursor(); '
    'cur.execute(\\"SELECT COUNT(*) FROM fills\\"); print(\\"fills:\\", cur.fetchone()[0]); conn.close()"'
)
print(stdout.read().decode())

# Current market countdown
stdin, stdout, stderr = ssh.exec_command('curl -s "http://127.0.0.1:9080/status"')
raw = stdout.read().decode()
try:
    d = json.loads(raw)
    mkt = d.get("market", {})
    print(f"Current market: {mkt.get('slug')} phase={mkt.get('phase')} countdown={mkt.get('countdown')}")
except:
    pass

# Recent log lines about fills/paper
stdin, stdout, stderr = ssh.exec_command(
    f'grep -i "paper\\|fill\\|execute\\|order.*state\\|SUBMIT" {REMOTE_BASE}/logs/runtime.log | grep -v "Book Unavailable" | tail -20'
)
print(f"\n=== PAPER/FILL LOGS ===\n{stdout.read().decode('utf-8', errors='replace')}")

ssh.close()
