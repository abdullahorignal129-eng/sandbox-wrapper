"""Randomized keepalive ping for the coordinator HF Space."""

import os
import random
import sys
import time

import httpx

# Sleep a random amount between 0 and 7200 seconds (2 hours)
def random_ping_delay(max_delay: float = 7200.0) -> float:
    return random.uniform(0, max_delay)

def main() -> int:
    url = os.environ.get("COORDINATOR_URL")
    secret = os.environ.get("GITHUB_SHARED_SECRET")
    if not url or not secret:
        print("COORDINATOR_URL and GITHUB_SHARED_SECRET must be set", file=sys.stderr)
        return 1

    delay = random_ping_delay()
    print(f"Sleeping {delay:.1f}s before ping...")
    time.sleep(delay)

    headers = {"Authorization": f"Bearer {secret}"}
    try:
        r = httpx.get(f"{url.rstrip('/')}/health", headers=headers, timeout=10)
        print(f"Ping status: {r.status_code} {r.text[:200]}")
        if r.status_code == 401:
            print("Authentication failed — check GITHUB_SHARED_SECRET", file=sys.stderr)
            return 1
        return 0
    except Exception as e:
        print(f"Ping failed: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
