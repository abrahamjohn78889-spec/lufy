"""Check MAJORITY and window activation logs."""
import paramiko
import sys

sys.stdout.reconfigure(encoding='utf-8')

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

# Get ALL MAJORITY-related logs
stdin, stdout, stderr = ssh.exec_command(f'grep -i "majority\\|window\\|open\\|trigger\\|intent\\|entry\\|band\\|direction\\|determined" {REMOTE_BASE}/logs/runtime.log | tail -60')
print("=== MAJORITY/WINDOW LOGS ===")
print(stdout.read().decode('utf-8', errors='replace'))

# Get full recent log
stdin, stdout, stderr = ssh.exec_command(f'tail -80 {REMOTE_BASE}/logs/runtime.log')
print("\n=== FULL RECENT LOG ===")
print(stdout.read().decode('utf-8', errors='replace'))

# Check windows table
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import sqlite3; conn = sqlite3.connect(\"data/arc.db\"); cur = conn.cursor(); '
    'cur.execute(\"SELECT * FROM windows ORDER BY rowid DESC LIMIT 10\"); '
    'cols = [d[0] for d in cur.description]; print(cols); '
    'for r in cur.fetchall(): print(r); '
    'conn.close()"'
)
print("\n=== WINDOWS TABLE ===")
print(stdout.read().decode('utf-8', errors='replace'))

ssh.close()
