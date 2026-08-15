"""Check orderbook availability and BTC observation flow."""
import paramiko
import time
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

def api(path):
    stdin, stdout, stderr = ssh.exec_command(f'curl -s "http://127.0.0.1:9080{path}"')
    return stdout.read().decode('utf-8', errors='replace')

# Check orderbook endpoint
print("=== ORDERBOOK ===")
ob_raw = api("/orderbook")
try:
    ob = json.loads(ob_raw)
    print(json.dumps(ob, indent=2)[:1000])
except Exception as e:
    print(f"Error: {e}, raw={ob_raw[:500]}")

# Check observation count now
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3; conn = sqlite3.connect(\"data/arc.db\"); cur = conn.cursor(); '
    'cur.execute(\"SELECT COUNT(*) FROM observations\"); print(\"Total observations:\", cur.fetchone()[0]); '
    'cur.execute(\"SELECT market_slug, COUNT(*), MAX(ts) FROM observations GROUP BY market_slug ORDER BY MAX(ts) DESC LIMIT 5\"); '
    'for r in cur.fetchall(): print(r); '
    'conn.close()"'
)
print(f"\n=== OBSERVATIONS ===\n{stdout.read().decode()}")

# Check intents
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3,json; conn = sqlite3.connect(\"data/arc.db\"); conn.row_factory = sqlite3.Row; cur = conn.cursor(); '
    'cur.execute(\"SELECT * FROM intents ORDER BY rowid DESC LIMIT 5\"); rows = [dict(r) for r in cur.fetchall()]; '
    'print(json.dumps(rows, indent=2)); conn.close()"'
)
print(f"\n=== INTENTS ===\n{stdout.read().decode()}")

# Get more log lines about MAJORITY
stdin, stdout, stderr = ssh.exec_command(f'grep -i "majority\\|entry\\|trade\\|intent\\|book" {REMOTE_BASE}/logs/runtime.log | tail -30')
print(f"\n=== MAJORITY LOGS ===\n{stdout.read().decode('utf-8', errors='replace')}")

# Full status
raw = api("/status")
try:
    d = json.loads(raw)
    rt = d.get("runtime", {})
    print(f"\ntrading_enabled: {rt.get('trading_enabled')}")
    print(f"execution_armed: {rt.get('execution_armed')}")
    print(f"feed_age_ms: {rt.get('feed_age_ms')}")
    failures = rt.get("gate_failures", [])
    if failures:
        print(f"gate_failures: {json.dumps(failures)}")
except Exception as e:
    print(f"Error: {e}")

ssh.close()
