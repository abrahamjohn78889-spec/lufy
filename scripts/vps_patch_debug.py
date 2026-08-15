"""Patch engine.py on VPS to log first 10 rejections, then restart."""
import paramiko
import time

HOST = "13.50.239.207"
USER = "roots"
PASS = "Amith@2002"
REMOTE_BASE = "/home/roots/arc_project"

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=15)

# Read current engine.py
stdin, stdout, stderr = ssh.exec_command(f'cat {REMOTE_BASE}/arc/runtime/engine.py')
engine_code = stdout.read().decode('utf-8', errors='replace')

# Find and patch _handle_message to log rejections
old_handle = '''    def _handle_message(self, message: Any, received_at: float) -> None:
        self._spec.offer(message)
        try:
            observation = self._validator.validate_payload(
                message, expected_symbol=EXPECTED_SYMBOL, received_at=received_at
            )
        except ObservationRejectedError:
            self.stats.observations_rejected += 1
            return'''

new_handle = '''    def _handle_message(self, message: Any, received_at: float) -> None:
        self._spec.offer(message)
        try:
            observation = self._validator.validate_payload(
                message, expected_symbol=EXPECTED_SYMBOL, received_at=received_at
            )
        except ObservationRejectedError as _exc:
            self.stats.observations_rejected += 1
            if self.stats.observations_rejected <= 10:
                import json as _json
                _msg_preview = _json.dumps(message)[:300] if isinstance(message, dict) else str(message)[:300]
                log_event(
                    logging.WARNING,
                    "Observation Rejected",
                    f"{_exc} | msg={_msg_preview}",
                    logger=self._logger,
                )
            return'''

if old_handle in engine_code:
    patched = engine_code.replace(old_handle, new_handle)
    stdin, stdout, stderr = ssh.exec_command(f'cat > {REMOTE_BASE}/arc/runtime/engine.py << \'ENDOFPATCH\'\n{patched}\nENDOFPATCH')
    stdout.read()
    print("Patched _handle_message with rejection logging")
else:
    print("ERROR: Could not find _handle_message pattern to patch")
    # Try to find it
    idx = engine_code.find('_handle_message')
    print(f"_handle_message found at char {idx}")
    if idx >= 0:
        print(engine_code[idx:idx+500])

ssh.close()
