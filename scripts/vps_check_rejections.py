"""Add temporary debug logging to see why observations are rejected."""
import paramiko

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

# Check current rejection count
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3; conn = sqlite3.connect(\"data/arc.db\"); cur = conn.cursor(); '
    'cur.execute(\"SELECT COUNT(*) FROM observations\"); print(\"Observations:\", cur.fetchone()[0]); '
    'conn.close()"'
)
print(stdout.read().decode().strip())

# Check API status for stats
stdin, stdout, stderr = ssh.exec_command('curl -s "http://127.0.0.1:9080/status"')
status = stdout.read().decode()
import json
try:
    d = json.loads(status)
    rt = d.get("runtime", {})
    print(f"\nStats from /status:")
    print(f"  trading_enabled: {rt.get('trading_enabled')}")
    print(f"  feed_age_ms: {rt.get('feed_age_ms')}")
    # Check if there are any stats about rejections
    for k in ["observations_accepted", "observations_rejected", "reconnects"]:
        v = rt.get(k)
        if v is not None:
            print(f"  {k}: {v}")
except Exception as e:
    print(f"Error: {e}")

# Get more log lines looking for rejection reasons
stdin, stdout, stderr = ssh.exec_command(f'grep -i "reject\\|error\\|fail\\|invalid" {REMOTE_BASE}/logs/runtime.log | tail -20')
log = stdout.read().decode('utf-8', errors='replace').strip()
print(f"\nRejection/Error log lines:\n{log if log else '(none found)'}")

# Check if the runtime is still running
stdin, stdout, stderr = ssh.exec_command('pgrep -af "arc run"')
pid = stdout.read().decode().strip()
print(f"\nRuntime PID: {pid}")

ssh.close()
