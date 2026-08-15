"""Upload gate fix script, run it, restart runtime, re-arm, verify."""
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

# Upload the fix script
local_script = f"{LOCAL_BASE}/scripts/vps_fix_gates.py"
remote_script = f"{REMOTE_BASE}/vps_fix_gates.py"
sftp.put(local_script, remote_script)
print("Uploaded vps_fix_gates.py")
sftp.close()

# Run it
stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 vps_fix_gates.py")
out = stdout.read().decode().strip()
err = stderr.read().decode().strip()
print(f"\n{out}")
if err:
    print(f"stderr: {err}")

# Restart runtime so new settings take effect
print("\n=== RESTARTING RUNTIME ===")
stdin, stdout, stderr = ssh.exec_command('pkill -f "arc run" || true')
stdout.read()
time.sleep(2)

stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && nohup /home/roots/.local/bin/arc run --mode=v1 > logs/runtime.log 2>&1 &'
)
time.sleep(8)

# Check PID
stdin, stdout, stderr = ssh.exec_command('pgrep -af "arc run"')
pid_out = stdout.read().decode().strip()
print(f"Runtime PID: {pid_out}")

# Re-bypass spec (restart may reset it)
stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 vps_fix_gates.py")
out2 = stdout.read().decode().strip()
print(f"\nPost-restart bypass:\n{out2}")

time.sleep(3)

# Arm execution
def api_cmd(path, method="GET"):
    if method == "POST":
        return f'curl -s -X POST "http://127.0.0.1:9080{path}"'
    return f'curl -s "http://127.0.0.1:9080{path}"'

stdin, stdout, stderr = ssh.exec_command(api_cmd("/strategies/MAJORITY/config?action=arm", "POST"))
arm_resp = stdout.read().decode()
print(f"\nArm response: {arm_resp[:300]}")

time.sleep(3)

# Final status check
stdin, stdout, stderr = ssh.exec_command(api_cmd("/status"))
status_raw = stdout.read().decode()
try:
    status = json.loads(status_raw)
    rt = status.get("runtime", {})
    print(f"\n=== FINAL STATUS ===")
    print(f"  phase: {rt.get('phase')}")
    print(f"  trading_enabled: {rt.get('trading_enabled')}")
    print(f"  execution_armed: {rt.get('execution_armed')}")
    print(f"  disable_reason: {rt.get('disable_reason')}")
    print(f"  feed_age_ms: {rt.get('feed_age_ms')}")
    print(f"  clock_drift_ms: {rt.get('clock_drift_ms')}")
    gs = rt.get("gates_summary", {})
    print(f"  gates: {json.dumps(gs, indent=4)}")
    gf = rt.get("gate_failures", [])
    if gf:
        print(f"  gate_failures: {json.dumps(gf, indent=4)}")
    else:
        print(f"  gate_failures: NONE — ALL GATES PASS")
except Exception as e:
    print(f"Parse error: {e}")
    print(f"Raw: {status_raw[:500]}")

# Recent log
print("\n=== RECENT LOG ===")
stdin, stdout, stderr = ssh.exec_command(f'tail -30 {REMOTE_BASE}/logs/runtime.log')
log_lines = stdout.read().decode().strip()
print(log_lines)

ssh.close()
print("\nDone.")
