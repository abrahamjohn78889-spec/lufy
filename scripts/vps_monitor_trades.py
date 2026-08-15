"""Monitor for first trade intent. Wait up to 10 minutes."""
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

print("Monitoring for first trade intent (up to 600s)...")
start = time.time()
last_check = ""

while time.time() - start < 600:
    elapsed = int(time.time() - start)

    # Check intents
    stdin, stdout, stderr = ssh.exec_command(
        f'cd {REMOTE_BASE} && python3 -c "'
        'import sqlite3; conn = sqlite3.connect(\"data/arc.db\"); cur = conn.cursor(); '
        'cur.execute(\"SELECT COUNT(*) FROM intents\"); print(cur.fetchone()[0]); conn.close()"'
    )
    count = stdout.read().decode().strip()

    # Get current market info
    raw = api("/status")
    try:
        d = json.loads(raw)
        rt = d.get("runtime", {})
        market = d.get("market", {})
        trading = rt.get("trading_enabled")
        armed = rt.get("execution_armed")
        slug = market.get("slug", "?")
        phase = market.get("phase", "?")
        countdown = market.get("countdown", "?")
        windows = market.get("windows", [])
        failures = rt.get("gate_failures", [])

        check_line = f"[{elapsed}s] intents={count} trading={trading} armed={armed} market={slug} phase={phase} countdown={countdown}"
        if check_line != last_check:
            print(check_line)
            if failures:
                print(f"  gate_failures: {json.dumps(failures)}")

            # Show window states
            for w in windows[:3]:
                state = w.get("state", "?")
                label = w.get("label", "?")
                direction = w.get("direction", "")
                opens_at = w.get("opens_at_display", {}).get("utc_display", "?")
                print(f"  {label}: state={state} dir={direction} opens_at={opens_at}")

            last_check = check_line

        if count and count != "0":
            print(f"\n*** TRADE INTENTS FOUND: {count} ***")
            # Get details
            stdin, stdout, stderr = ssh.exec_command(
                f'cd {REMOTE_BASE} && python3 -c "'
                'import sqlite3,json; conn = sqlite3.connect(\"data/arc.db\"); conn.row_factory = sqlite3.Row; '
                'cur = conn.cursor(); cur.execute(\"SELECT * FROM intents ORDER BY rowid DESC LIMIT 5\"); '
                'rows = [dict(r) for r in cur.fetchall()]; print(json.dumps(rows, indent=2)); conn.close()"'
            )
            print(stdout.read().decode('utf-8', errors='replace'))
            break

    except Exception as e:
        if elapsed % 60 == 0:
            print(f"[{elapsed}s] Status error: {e}")

    time.sleep(15)

# Final log
stdin, stdout, stderr = ssh.exec_command(f'grep -i "majority\\|intent\\|trade\\|entry" {REMOTE_BASE}/logs/runtime.log | tail -40')
print(f"\n=== RECENT MAJORITY LOGS ===\n{stdout.read().decode('utf-8', errors='replace')}")

ssh.close()
