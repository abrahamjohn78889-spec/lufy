"""Deploy updated engine.py (with _late_ptb_retry) to VPS and restart."""
import paramiko, sys, time
sys.stdout.reconfigure(encoding="utf-8")

LOCAL = r"C:\Users\AMITH\OneDrive\Desktop\lufy\arc\runtime\engine.py"
REMOTE = "/home/roots/arc_project/arc/runtime/engine.py"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("13.50.239.207", username="roots", password="Amith@2002", timeout=15)

sftp = ssh.open_sftp()
print("Uploading engine.py ...")
sftp.put(LOCAL, REMOTE)
sftp.close()
print("Upload done.")

# Restart
_, o, _ = ssh.exec_command("pkill -f 'arc run'; sleep 2")
o.channel.recv_exit_status()
print("Old process killed.")

_, o, _ = ssh.exec_command(
    "cd /home/roots/arc_project && nohup /home/roots/.local/bin/arc run --mode=v1 > logs/runtime.log 2>&1 &"
)
time.sleep(5)

# Verify startup
_, o, _ = ssh.exec_command("tail -30 /home/roots/arc_project/logs/runtime.log")
out = o.read().decode("utf-8", errors="replace")
print(out)

# Arm MAJORITY
_, o, _ = ssh.exec_command(
    "curl -s -X POST http://127.0.0.1:9080/strategies/MAJORITY/config?action=arm"
)
arm_out = o.read().decode("utf-8", errors="replace")
print("ARM:", arm_out)

ssh.close()
print("Done.")
