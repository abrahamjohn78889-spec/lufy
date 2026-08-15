"""Get strategies list and arm trading."""
import httpx
import json

base = "http://127.0.0.1:9080"

# Get strategies
r = httpx.get(f"{base}/strategies", timeout=5)
print("=== STRATEGIES ===")
print(json.dumps(r.json(), indent=2)[:1000])

# Get the strategy ID
strategies = r.json()
if isinstance(strategies, list) and strategies:
    sid = strategies[0].get("id", strategies[0].get("strategy_id", ""))
elif isinstance(strategies, dict):
    sid = list(strategies.keys())[0] if strategies else ""
else:
    sid = "MAJORITY"

print(f"\nUsing strategy_id: {sid}")

# Arm it
r2 = httpx.post(f"{base}/strategies/{sid}/config?action=arm", timeout=5)
print(f"\nARM response: {r2.status_code}")
print(json.dumps(r2.json(), indent=2)[:500])

# Verify
r3 = httpx.get(f"{base}/status", timeout=5)
d3 = r3.json()
rt = d3.get("runtime", {})
print(f"\n=== AFTER ARMING ===")
print(f"  trading_enabled: {rt.get('trading_enabled')}")
print(f"  execution_armed: {rt.get('execution_armed')}")
print(f"  disable_reason: {rt.get('disable_reason')}")
print(f"  gates_summary: {rt.get('gates_summary')}")
print(f"  gate_failures: {json.dumps(rt.get('gate_failures', []), indent=4)}")
