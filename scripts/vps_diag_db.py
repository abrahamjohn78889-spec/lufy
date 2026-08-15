"""Deep check: observations, rotator routing, and next market cycle."""
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

# Direct DB check with explicit error handling
diag_script = """
import sqlite3, time, json

conn = sqlite3.connect('data/arc.db')
cur = conn.cursor()

print("=== TABLES ===")
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print(tables)

print("\\n=== OBSERVATIONS COUNT ===")
try:
    cur.execute("SELECT COUNT(*) FROM observations")
    print("observations:", cur.fetchone()[0])
except Exception as e:
    print("ERROR:", e)

print("\\n=== OBSERVATIONS SCHEMA ===")
try:
    cur.execute("PRAGMA table_info(observations)")
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print("ERROR:", e)

print("\\n=== LATEST OBSERVATIONS ===")
try:
    cur.execute("SELECT * FROM observations ORDER BY id DESC LIMIT 5")
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print("ERROR:", e)

print("\\n=== MARKETS ===")
try:
    cur.execute("SELECT slug, phase, window_ts, close_ts, observation_count FROM markets ORDER BY close_ts DESC LIMIT 5")
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print("ERROR:", e)

print("\\n=== INTENTS ===")
try:
    cur.execute("SELECT COUNT(*) FROM intents")
    print("intents:", cur.fetchone()[0])
except Exception as e:
    print("ERROR:", e)

print("\\n=== RUNTIME STATE ===")
try:
    cur.execute("SELECT key, value FROM runtime_state")
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print("ERROR:", e)

print("\\n=== SETTINGS (majority) ===")
try:
    cur.execute("SELECT key, value FROM settings WHERE key LIKE '%majority%' OR key LIKE '%entry%' OR key LIKE '%buffer%' OR key LIKE '%trigger%' OR key LIKE '%window%'")
    for row in cur.fetchall():
        print(row)
except Exception as e:
    print("ERROR:", e)

conn.close()
"""

# Write diag script to VPS
stdin, stdout, stderr = ssh.exec_command(f"cat > {REMOTE_BASE}/diag_db.py << 'PYEOF'\n{diag_script}\nPYEOF")
stdout.read()

stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 diag_db.py")
out = stdout.read().decode('utf-8', errors='replace')
err = stderr.read().decode('utf-8', errors='replace')
print(out)
if err:
    print(f"STDERR: {err}")

# Current time and market timing
stdin, stdout, stderr = ssh.exec_command(
    f'cd {REMOTE_BASE} && python3 -c "'
    'import time; now=int(time.time()); '
    'wts=(now//300)*300; '
    'print(f\"now={now} window_ts={wts} close_ts={wts+300} secs_to_close={wts+300-now}\"); '
    'print(f\"current_slug=btc-updown-5m-{wts}\")"'
)
print(f"\n=== TIMING ===\n{stdout.read().decode()}")

ssh.close()
