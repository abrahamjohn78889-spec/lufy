"""Upload and run parse diagnostic on VPS."""
import paramiko

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"
LOCAL_BASE = "C:/Users/AMITH/OneDrive/Desktop/lufy"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)
sftp = ssh.open_sftp()
sftp.put(f"{LOCAL_BASE}/scripts/vps_diag_parse.py", f"{REMOTE_BASE}/diag_parse.py")
sftp.close()

stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 diag_parse.py", timeout=15)
out = stdout.read().decode('utf-8', errors='replace')
err = stderr.read().decode('utf-8', errors='replace')
print(out)
if err:
    print(f"\nSTDERR:\n{err}")

ssh.close()
