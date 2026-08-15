"""Check full status and find arm endpoint."""
import httpx
import json

base = "http://127.0.0.1:9080"

# Full status
r = httpx.get(f"{base}/status", timeout=5)
print("=== FULL STATUS ===")
print(json.dumps(r.json(), indent=2)[:2000])
