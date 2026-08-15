"""Check current trade state - intents, ledger, live status."""
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

# Check intents
diag = """
import sqlite3, json
conn = sqlite3.connect('data/arc.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== INTENTS ===")
cur.execute("SELECT * FROM intents ORDER BY rowid DESC LIMIT 10")
rows = [dict(r) for r in cur.fetchall()]
for r in rows:
    print(json.dumps(r, indent=2))
print(f"Total intents: {len(rows)}")

print("\\n=== LEDGER ===")
try:
    cur.execute("SELECT * FROM ledger ORDER BY rowid DESC LIMIT 10")
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        print(json.dumps(r, indent=2))
    print(f"Total ledger entries: {len(rows)}")
except Exception as e:
    print(f"No ledger table or error: {e}")

print("\\n=== TRADES ===")
try:
    cur.execute("SELECT * FROM trades ORDER BY rowid DESC LIMIT 10")
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        print(json.dumps(r, indent=2))
    print(f"Total trades: {len(rows)}")
except Exception as e:
    print(f"No trades table or error: {e}")

print("\\n=== MARKETS (recent) ===")
cur.execute("SELECT slug, phase, window_ts, close_ts, ptb, observation_count FROM markets ORDER BY close_ts DESC LIMIT 5")
for r in cur.fetchall():
    print(dict(r))

conn.close()
"""

stdin, stdout, stderr = ssh.exec_command(f"cat > {REMOTE_BASE}/check_trades.py << 'PYEOF'\n{diag}\nPYEOF")
stdout.read()

stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 check_trades.py")
out = stdout.read().decode('utf-8', errors='replace')
err = stderr.read().decode('utf-8', errors='replace')
print(out)
if err:
    print(f"STDERR: {err}")

# Current status
stdin, stdout, stderr = ssh.exec_command('curl -s "http://127.0.0.1:9080/status"')
raw = stdout.read().decode()
try:
    d = json.loads(raw)
    rt = d.get("runtime", {})
    mkt = d.get("market", {})
    print(f"\n=== LIVE STATUS ===")
    print(f"trading_enabled: {rt.get('trading_enabled')}")
    print(f"execution_armed: {rt.get('execution_armed')}")
    print(f"market: {mkt.get('slug')} phase={mkt.get('phase')} countdown={mkt.get('countdown')}")
    print(f"feed_age_ms: {rt.get('feed_age_ms')}")
    failures = rt.get("gate_failures", [])
    if failures:
        print(f"gate_failures: {json.dumps(failures)}")
except Exception as e:
    print(f"Status error: {e}, raw={raw[:300]}")

# Count MAJORITY submissions vs no-trade
stdin, stdout, stderr = ssh.exec_command(
    f'echo "=== SUBMITTED ===" && grep -c "MAJORITY Submitted" {REMOTE_BASE}/logs/runtime.log; '
    f'echo "=== NO TRADE ===" && grep -c "MAJORITY No Trade" {REMOTE_BASE}/logs/runtime.log; '
    f'echo "=== INTENT CREATED ===" && grep -c "MAJORITY Intent Created" {REMOTE_BASE}/logs/runtime.log'
)
print(f"\n=== COUNTS ===\n{stdout.read().decode()}")

ssh.close()
