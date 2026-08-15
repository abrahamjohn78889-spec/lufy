import paramiko, time, sys
sys.stdout.reconfigure(encoding="utf-8")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)
time.sleep(60)
_, o, _ = ssh.exec_command("tail -40 /home/roots/arc_project/logs/runtime.log")
print(o.read().decode("utf-8", errors="replace"))
ssh.close()
