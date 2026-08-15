"""Upload and run RTDS debug script on VPS."""
import paramiko
import time

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"
LOCAL_BASE = "C:/Users/AMITH/OneDrive/Desktop/lufy"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)
sftp = ssh.open_sftp()

sftp.put(f"{LOCAL_BASE}/scripts/vps_debug_rtds.py", f"{REMOTE_BASE}/debug_rtds.py")
print("Uploaded debug_rtds.py")
sftp.close()

# Run it (takes ~30 seconds)
stdin, stdout, stderr = ssh.exec_command(f"cd {REMOTE_BASE} && python3 debug_rtds.py", timeout=45)
out = stdout.read().decode('utf-8', errors='replace')
err = stderr.read().decode('utf-8', errors='replace')
print(out)
if err:
    print(f"\nSTDERR:\n{err}")

ssh.close()
