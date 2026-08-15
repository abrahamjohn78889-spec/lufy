"""Upload and run improved RTDS debug script."""
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
sftp.put(f"{LOCAL_BASE}/scripts/vps_debug_rtds2.py", f"{REMOTE_BASE}/debug_rtds2.py")
print("Uploaded")
sftp.close()

stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 debug_rtds2.py", timeout=50)
out = stdout.read().decode('utf-8', errors='replace')
err = stderr.read().decode('utf-8', errors='replace')
print(out)
if err:
    print(f"\nSTDERR:\n{err}")

ssh.close()
