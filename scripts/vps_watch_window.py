"""Watch one window lifecycle end-to-end on VPS (fill bridge live)."""
import paramiko
import time
import sys

sys.stdout.reconfigure(encoding="utf-8")

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

def run(cmd):
    _, out, err = ssh.exec_command(cmd)
    return out.read().decode("utf-8", errors="replace"), err.read().decode("utf-8", errors="replace")

# Snapshot BEFORE
out, _ = run(f"cd {REMOTE} && python3 -c 'import sqlite3;c=sqlite3.connect(\"data/arc.db\");print(\"orders\",c.execute(\"SELECT COUNT(*) FROM orders\").fetchone()[0]);print(\"fills\",c.execute(\"SELECT COUNT(*) FROM fills\").fetchone()[0]);print(\"settlements\",c.execute(\"SELECT COUNT(*) FROM settlements\").fetchone()[0])'")
print("=== BEFORE ===")
print(out)

# Tail log for Paper Fill / No Trade / MAJORITY events while window progresses
print("=== WATCHING LOG (tail -f style, 90s) ===")
for i in range(9):
    time.sleep(10)
    out, _ = run(f"tail -4 {REMOTE}/logs/runtime.log | grep -E 'Fill|Order|MAJORITY|No Trade|window|Settled|CANCELLED' || tail -2 {REMOTE}/logs/runtime.log")
    print(f"[+{(i+1)*10}s] {out.strip()[:200]}")

ssh.close()
