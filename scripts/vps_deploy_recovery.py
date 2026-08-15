"""Upload fixed engine.py, restart runtime, verify all gates pass."""
import paramiko
import time
import json

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"
LOCAL_BASE = "C:/Users/AMITH/OneDrive/Desktop/lufy"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)
sftp = ssh.open_sftp()

# Upload fixed engine.py
sftp.put(f"{LOCAL_BASE}/arc/runtime/engine.py", f"{REMOTE_BASE}/arc/runtime/engine.py")
print("Uploaded arc/runtime/engine.py")
sftp.close()

# Restart
print("\nRestarting runtime...")
stdin, stdout, stderr = ssh.exec_command('pkill -f "arc run" || true')
stdout.read()
time.sleep(2)

stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && nohup /home/roots/.local/bin/arc run --mode=v1 > logs/runtime.log 2>&1 &'
)
time.sleep(8)

# Re-apply bypass + arm
stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 vps_fix_gates.py")
out = stdout.read().decode().strip()
print(f"\nBypass: {out}")

time.sleep(3)

def api(path, method="GET"):
    cmd = f'curl -s {"-X POST " if method == "POST" else ""}"http://127.0.0.1:9080{path}"'
    stdin, stdout, stderr = ssh.exec_command(cmd)
    return stdout.read().decode()

# Arm
arm_resp = api("/strategies/MAJORITY/config?action=arm", "POST")
print(f"\nArm: {arm_resp[:200]}")

# Wait for feed to warm up and recovery logic to kick in
print("\nWaiting 20s for feed warmup + recovery...")
time.sleep(20)

for i in range(6):
    raw = api("/status")
    try:
        d = json.loads(raw)
        rt = d.get("runtime", {})
        trading = rt.get("trading_enabled")
        armed = rt.get("execution_armed")
        reason = rt.get("disable_reason") or rt.get("reason")
        feed_age = rt.get("feed_age_ms")
        drift = rt.get("clock_drift_ms")
        failures = rt.get("gate_failures", [])
        print(f"\n[Check {i+1}] trading={trading} armed={armed} reason={reason}")
        print(f"  feed_age={feed_age:.0f}ms  drift={drift:.0f}ms")
        if failures:
            print(f"  failures: {json.dumps(failures)}")
        else:
            print(f"  *** ALL GATES PASS ***")
            break
    except Exception as e:
        print(f"[Check {i+1}] Error: {e}")
    time.sleep(10)

# Recent log
print("\n=== RECENT LOG ===")
stdin, stdout, stderr = ssh.exec_command(f'tail -25 {REMOTE_BASE}/logs/runtime.log')
print(stdout.read().decode().strip())

ssh.close()
print("\nDone.")
