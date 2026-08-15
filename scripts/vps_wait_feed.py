"""Wait for feed to warm up, then check status."""
import paramiko
import time
import json

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

def api_get(path):
    stdin, stdout, stderr = ssh.exec_command(f'curl -s "http://127.0.0.1:9080{path}"')
    return stdout.read().decode()

# Wait for feed to settle
print("Waiting 15s for feed to warm up...")
time.sleep(15)

for i in range(4):
    raw = api_get("/status")
    try:
        d = json.loads(raw)
        rt = d.get("runtime", {})
        phase = rt.get("phase")
        trading = rt.get("trading_enabled")
        armed = rt.get("execution_armed")
        reason = rt.get("disable_reason")
        feed_age = rt.get("feed_age_ms")
        drift = rt.get("clock_drift_ms")
        gates = rt.get("gates_summary", {})
        failures = rt.get("gate_failures", [])
        print(f"\n[Check {i+1}] phase={phase} trading={trading} armed={armed}")
        print(f"  feed_age={feed_age:.0f}ms  drift={drift:.0f}ms  reason={reason}")
        print(f"  gates={json.dumps(gates)}")
        if failures:
            print(f"  failures: {json.dumps(failures)}")
        else:
            print(f"  *** ALL GATES PASS ***")
    except Exception as e:
        print(f"[Check {i+1}] Error: {e}, raw={raw[:200]}")
    time.sleep(10)

# Check observations flowing
stdin, stdout, stderr = ssh.exec_command('tail -15 /home/roots/arc_project/logs/runtime.log')
print(f"\n=== RECENT LOG ===\n{stdout.read().decode().strip()}")

ssh.close()
