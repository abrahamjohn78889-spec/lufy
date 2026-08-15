"""Arm trading and check status on VPS."""
import httpx
import json

base = "http://127.0.0.1:9080"

# Check status before
r = httpx.get(f"{base}/status", timeout=5)
d = r.json()
print("=== BEFORE ARMING ===")
for k in ["trading_enabled", "execution_armed", "disable_reason", "spec_status", "gates_summary"]:
    print(f"  {k}: {d.get(k)}")

# Arm execution
print("\n=== POST /start ===")
r2 = httpx.post(f"{base}/start", timeout=5)
print(f"Status: {r2.status_code}")
print(json.dumps(r2.json(), indent=2)[:500])

# Check status after
r3 = httpx.get(f"{base}/status", timeout=5)
d3 = r3.json()
print("\n=== AFTER ARMING ===")
for k in ["trading_enabled", "execution_armed", "disable_reason", "spec_status", "gates_summary", "gates_passing"]:
    print(f"  {k}: {d3.get(k)}")
