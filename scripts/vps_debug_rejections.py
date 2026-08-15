"""Upload patched engine, restart runtime, wait for rejection logs."""
import paramiko
import time
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"
LOCAL_BASE = "C:/Users/AMITH/OneDrive/Desktop/lufy"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)
sftp = ssh.open_sftp()

# Upload patched engine.py
sftp.put(f"{LOCAL_BASE}/arc/runtime/engine.py", f"{REMOTE_BASE}/arc/runtime/engine.py")
print("Uploaded patched engine.py")
sftp.close()

# Restart
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
print(f"Bypass: {out}")

time.sleep(2)

stdin, stdout, stderr = ssh.exec_command(
    'curl -s -X POST "http://127.0.0.1:9080/strategies/MAJORITY/config?action=arm"'
)
print(f"Arm: {stdout.read().decode()}")

# Wait 15 seconds for observations to flow (or be rejected)
print("\nWaiting 15s for observations...")
time.sleep(15)

# Check rejection logs
stdin, stdout, stderr = ssh.exec_command(f'grep -i "rejected\\|observation" {REMOTE_BASE}/logs/runtime.log | tail -20')
log = stdout.read().decode('utf-8', errors='replace').strip()
print(f"\n=== REJECTION LOGS ===\n{log if log else '(none)'}")

# Check stats
stdin, stdout, stderr = ssh.exec_command('curl -s "http://127.0.0.1:9080/status"')
status_raw = stdout.read().decode()
try:
    d = json.loads(status_raw)
    rt = d.get("runtime", {})
    print(f"\ntrading_enabled: {rt.get('trading_enabled')}")
    print(f"feed_age_ms: {rt.get('feed_age_ms')}")
    failures = rt.get("gate_failures", [])
    if failures:
        print(f"gate_failures: {json.dumps(failures)}")
except Exception as e:
    print(f"Error: {e}")

# Full recent log
stdin, stdout, stderr = ssh.exec_command(f'tail -30 {REMOTE_BASE}/logs/runtime.log')
print(f"\n=== RECENT LOG ===\n{stdout.read().decode('utf-8', errors='replace').strip()}")

ssh.close()
