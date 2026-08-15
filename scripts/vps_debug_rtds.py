"""Connect to RTDS WebSocket directly and print raw messages."""
import asyncio
import json
import websockets
import sys

sys.stdout.reconfigure(encoding='utf-8')

RTDS_URL = "wss://ws-live-data.polymarket.com"

async def main():
    print(f"Connecting to {RTDS_URL}...")
    async with websockets.connect(RTDS_URL, ping_interval=20, ping_timeout=10) as ws:
        print("Connected!")

        # Send subscription for crypto prices
        sub_msg = json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "update"},
                {"topic": "crypto_prices", "type": "update"},
            ]
        })
        await ws.send(sub_msg)
        print(f"Sent: {sub_msg}")

        # Listen for 30 seconds
        count = 0
        import time
        start = time.time()
        while time.time() - start < 30:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                count += 1
                parsed = json.loads(msg)

                # Print first 20 messages in full
                if count <= 20:
                    print(f"\n--- Message {count} ---")
                    print(json.dumps(parsed, indent=2)[:1000])
                else:
                    # Just count
                    pass

                # Check for BTC/USD
                if isinstance(parsed, dict):
                    symbol = parsed.get("symbol") or parsed.get("pair") or ""
                    if "BTC" in str(symbol).upper():
                        print(f"\n*** BTC MESSAGE #{count} ***")
                        print(json.dumps(parsed, indent=2)[:500])

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Error: {e}")
                break

        print(f"\nTotal messages received: {count}")

asyncio.run(main())
