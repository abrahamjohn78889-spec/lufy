import paramiko, sys, json
sys.stdout.reconfigure(encoding="utf-8")

REMOTE = '''
import sqlite3, json
c = sqlite3.connect("data/arc.db")
c.row_factory = sqlite3.Row
# Check 1786794000 specifically
row = c.execute("SELECT * FROM markets WHERE slug='btc-updown-5m-1786794000'").fetchone()
print("MARKET_1786794000:", json.dumps(dict(row), indent=2) if row else "NONE")
# Count settlements
cnt = c.execute("SELECT COUNT(*) as n FROM settlements").fetchone()
print("TOTAL_SETTLEMENTS:", cnt["n"])
# All phases
rows = c.execute("SELECT phase, COUNT(*) as n FROM markets GROUP BY phase").fetchall()
print("PHASE_COUNTS:", json.dumps([dict(r) for r in rows]))
'''

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)
sftp = ssh.open_sftp()
with sftp.open("/home/roots/arc_project/_probe.py", "w") as f:
    f.write(REMOTE)
sftp.close()
_, o, _ = ssh.exec_command("cd /home/roots/arc_project && python3 _probe.py")
print(o.read().decode("utf-8", errors="replace"))
ssh.exec_command("rm /home/roots/arc_project/_probe.py")
ssh.close()
