"""Diagnose observation parsing by importing ARC modules directly."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '/home/roots/arc_project')

import json
from arc.market.validation import parse_payload, ObservationValidator, ValidationLimits
from arc.market.validation import ObservationRejectedError

# Simulate an RTDS message
rtds_message = {
    "connection_id": "test",
    "payload": {
        "full_accuracy_value": "62890840380997925000000",
        "symbol": "btc/usd",
        "timestamp": 1786785354000,
        "value": 62890.84038099793
    },
    "timestamp": 1786785355163,
    "topic": "crypto_prices_chainlink",
    "type": "update"
}

# Extract inner message like _messages_in does
inner = rtds_message.get("payload", rtds_message.get("data"))
print(f"Inner message: {json.dumps(inner, indent=2)}")

# Try to parse
try:
    obs = parse_payload(inner, expected_symbol="BTC/USD")
    print(f"\nParsed OK:")
    print(f"  ts={obs.ts}")
    print(f"  price={obs.price}")
    print(f"  feed_id={obs.feed_id}")
    print(f"  window_seconds={obs.window_seconds}")
except ObservationRejectedError as e:
    print(f"\nPARSE REJECTED: {e}")

# Try validation
import time
validator = ObservationValidator(limits=ValidationLimits())
received_at = time.time()
try:
    validated = validator.validate(obs, received_at=received_at)
    print(f"\nValidated OK: {validated}")
except ObservationRejectedError as e:
    print(f"\nVALIDATION REJECTED: {e}")
    print(f"  received_at={received_at}")
    print(f"  obs.ts={obs.ts}")
    print(f"  diff={(received_at - obs.ts)*1000:.0f}ms")
