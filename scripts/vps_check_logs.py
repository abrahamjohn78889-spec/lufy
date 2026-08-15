"""Check VPS logs for feed issues and observation flow."""
import paramiko
import sys

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

# Force UTF-8 output
sys.stdout.reconfigure(encoding='utf-8')

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

# Get full recent log (last 100 lines)
stdin, stdout, stderr = ssh.exec_command(f'tail -100 {REMOTE_BASE}/logs/runtime.log')
log = stdout.read().decode('utf-8', errors='replace').strip()
print("=== LAST 100 LOG LINES ===")
print(log)

# Check observation timestamps
print("\n=== OBSERVATION TIMESTAMPS ===")
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3; '
    'conn = sqlite3.connect(\"data/arc.db\"); '
    'cur = conn.cursor(); '
    'cur.execute(\"SELECT ts, received_at FROM observations ORDER BY id DESC LIMIT 20\"); '
    'for row in cur.fetchall(): print(row); '
    'conn.close()"'
)
print(stdout.read().decode('utf-8', errors='replace').strip())

# Check runtime state
print("\n=== RUNTIME STATE ===")
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3; '
    'conn = sqlite3.connect(\"data/arc.db\"); '
    'cur = conn.cursor(); '
    'cur.execute(\"SELECT key, value FROM runtime_state\"); '
    'for row in cur.fetchall(): print(row); '
    'conn.close()"'
)
print(stdout.read().decode('utf-8', errors='replace').strip())

ssh.close()
