import paramiko, sys, json
sys.stdout.reconfigure(encoding="utf-8")

REMOTE = '''
import sqlite3, json
c = sqlite3.connect("data/arc.db")
c.row_factory = sqlite3.Row
for slug in ["btc-updown-5m-1786791000", "btc-updown-5m-1786791300"]:
    row = c.execute("SELECT close_ts FROM markets WHERE slug=?", (slug,)).fetchone()
    if not row:
        print(f"{slug}: NO MARKET ROW")
        continue
    close_ts = int(row["close_ts"])
    window_start = close_ts - 30
    obs = c.execute("SELECT COUNT(*) as cnt FROM observations WHERE market_slug=? AND ts>=? AND ts<=?", (slug, window_start, close_ts+1)).fetchone()
    print(f"{slug}: close={close_ts} settlement_window_obs={obs['cnt']}")
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
