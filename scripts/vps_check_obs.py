import paramiko, sys, json
sys.stdout.reconfigure(encoding="utf-8")

REMOTE = '''
import sqlite3, json
c = sqlite3.connect("data/arc.db")
c.row_factory = sqlite3.Row
rows = c.execute("SELECT market_slug, COUNT(*) as cnt FROM observations GROUP BY market_slug ORDER BY market_slug DESC LIMIT 10").fetchall()
print("OBS_COUNTS:", json.dumps([dict(r) for r in rows], indent=2))
rows = c.execute("SELECT DISTINCT f.market_slug FROM fills f JOIN markets m ON f.market_slug = m.slug WHERE m.phase = 'SETTLING'").fetchall()
print("UNSETTLED_WITH_FILLS:", [r["market_slug"] for r in rows])
rows = c.execute("SELECT DISTINCT market_slug FROM fills").fetchall()
print("ALL_FILL_SLUGS:", [r["market_slug"] for r in rows])
'''

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)
sftp = ssh.open_sftp()
with sftp.open("/home/roots/arc_project/_probe.py", "w") as f:
    f.write(REMOTE)
sftp.close()
_, o, e = ssh.exec_command("cd /home/roots/arc_project && python3 _probe.py")
print(o.read().decode("utf-8", errors="replace"))
err = e.read().decode("utf-8", errors="replace")
if err:
    print("ERR:", err[:500])
ssh.exec_command("rm /home/roots/arc_project/_probe.py")
ssh.close()
