"""Wait for trade intents to appear and report status."""
import paramiko
import time
import json

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

def api(path):
    stdin, stdout, stderr = ssh.exec_command(f'curl -s "http://127.0.0.1:9080{path}"')
    return stdout.read().decode()

def db_query(query):
    stdin, stdout, stderr = ssh.exec_command(
        f'cd {REMOTE_BASE} && python3 -c "import sqlite3; conn=sqlite3.connect(\'data/arc.db\'); cur=conn.cursor(); cur.execute(\'{query}\'); print(cur.fetchall()); conn.close()"'
    )
    return stdout.read().decode().strip()

print("=== INITIAL STATE ===")
print(f"Intents: {db_query('SELECT COUNT(*) FROM intents')}")
print(f"Ledger: {db_query('SELECT COUNT(*) FROM ledger')}")

# Wait for a market window to trigger (max ~5 min)
print("\nWaiting up to 300s for trade intents...")
start = time.time()
found = False
while time.time() - start < 300:
    # Check intents
    stdin, stdout, stderr = ssh.exec_command(f'cd {REMOTE_BASE} && python3 -c "import sqlite3; conn=sqlite3.connect(\'data/arc.db\'); cur=conn.cursor(); cur.execute(\'SELECT COUNT(*) FROM intents\'); print(cur.fetchone()[0]); conn.close()"')
    count = stdout.read().decode().strip()
    elapsed = int(time.time() - start)

    if count and count != "0":
        print(f"\n[{elapsed}s] FOUND {count} intents!")
        found = True
        break

    # Check gates too
    raw = api("/status")
    try:
        d = json.loads(raw)
        rt = d.get("runtime", {})
        trading = rt.get("trading_enabled")
        armed = rt.get("execution_armed")
        failures = rt.get("gate_failures", [])
        market = d.get("market", {})
        phase = market.get("phase")

        if elapsed % 30 == 0:
            fail_str = json.dumps(failures) if failures else "none"
            print(f"  [{elapsed}s] trading={trading} armed={armed} market_phase={phase} gate_failures={fail_str}")
    except:
        pass

    time.sleep(10)

if not found:
    print("\nNo intents after 300s.")

# Get detailed status
print("\n=== STATUS ===")
raw = api("/status")
try:
    d = json.loads(raw)
    rt = d.get("runtime", {})
    print(f"  trading_enabled: {rt.get('trading_enabled')}")
    print(f"  execution_armed: {rt.get('execution_armed')}")
    print(f"  feed_age_ms: {rt.get('feed_age_ms')}")
    print(f"  gates: {rt.get('gates_summary')}")
    failures = rt.get("gate_failures", [])
    if failures:
        print(f"  gate_failures: {json.dumps(failures, indent=4)}")
    market = d.get("market", {})
    print(f"  market: {json.dumps(market, indent=4)}")
except Exception as e:
    print(f"  Error: {e}")

# Recent log
print("\n=== RECENT LOG (last 40 lines) ===")
stdin, stdout, stderr = ssh.exec_command(f'tail -40 {REMOTE_BASE}/logs/runtime.log')
print(stdout.read().decode().strip())

# Check intents detail
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "import sqlite3,json; conn=sqlite3.connect(\'data/arc.db\'); conn.row_factory=sqlite3.Row; cur=conn.cursor(); cur.execute(\'SELECT * FROM intents ORDER BY rowid DESC LIMIT 5\'); rows=[dict(r) for r in cur.fetchall()]; print(json.dumps(rows, indent=2)); conn.close()"'
)
print(f"\n=== INTENTS ===\n{stdout.read().decode().strip()}")

# Check ledger
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "import sqlite3,json; conn=sqlite3.connect(\'data/arc.db\'); conn.row_factory=sqlite3.Row; cur=conn.cursor(); cur.execute(\'SELECT * FROM ledger ORDER BY rowid DESC LIMIT 5\'); rows=[dict(r) for r in cur.fetchall()]; print(json.dumps(rows, indent=2)); conn.close()"'
)
print(f"\n=== LEDGER ===\n{stdout.read().decode().strip()}")

ssh.close()
