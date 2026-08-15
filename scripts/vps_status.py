import paramiko
import sys
sys.stdout.reconfigure(encoding="utf-8")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)
_, o, _ = ssh.exec_command("curl -s http://127.0.0.1:9080/status")
print(o.read().decode("utf-8", errors="replace")[:1500])
ssh.close()
