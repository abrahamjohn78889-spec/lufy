import paramiko, sys
sys.stdout.reconfigure(encoding="utf-8")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)
_, o, _ = ssh.exec_command("grep -E '1786791000|1786791300' /home/roots/arc_project/logs/runtime.log | tail -20")
print(o.read().decode("utf-8", errors="replace"))
ssh.close()
