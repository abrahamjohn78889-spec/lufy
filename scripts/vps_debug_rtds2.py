"""Connect to RTDS WebSocket and print raw messages with better error handling."""
import asyncio
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

RTDS_URL = "wss://ws-live-data.polymarket.com"

async def main():
    import websockets
    print(f"Connecting to {RTDS_URL}...")
    async with websockets.connect(RTDS_URL, ping_interval=20, ping_timeout=10) as ws:
        print("Connected!")

        # Try the exact subscription format from ARC code
        sub_msg = json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "update"},
            ]
        })
        await ws.send(sub_msg)
        print(f"Sent subscription: {sub_msg}")

        # Listen for 30 seconds, handle all frame types
        count = 0
        import time
        start = time.time()
        while time.time() - start < 35:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                count += 1

                if isinstance(msg, bytes):
                    print(f"\n[Msg {count}] BINARY ({len(msg)} bytes): {msg[:200]}")
                    try:
                        parsed = json.loads(msg.decode('utf-8'))
                        print(json.dumps(parsed, indent=2)[:500])
                    except:
                        pass
                else:
                    print(f"\n[Msg {count}] TEXT ({len(msg)} chars)")
                    try:
                        parsed = json.loads(msg)
                        print(json.dumps(parsed, indent=2)[:800])
                    except Exception as e:
                        print(f"  Not JSON: {e}")
                        print(f"  Raw: {msg[:300]}")

            except asyncio.TimeoutError:
                elapsed = int(time.time() - start)
                print(f"  [{elapsed}s] No message in 5s window...")
            except Exception as e:
                print(f"Error: {type(e).__name__}: {e}")
                break

        print(f"\nTotal messages received: {count}")

asyncio.run(main())
