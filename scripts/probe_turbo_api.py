"""
Probe the Bluefors REST API for turbo pump endpoints.

We know:
  - The compressor status endpoint is ``bflegacy/double/cparun``
  - The pulse tube endpoint is ``bf/pulsetube``
  - The log status keys for turbos include ``tc400pumpstatn``, ``tc400actualspd``,
    ``tc400drvpower`` (and their ``_2`` variants for the second pump).

Hypothesis: the live API follows the same ``bflegacy/double/<log_key>``
pattern that cparun uses.  This script tries every plausible path and
prints which ones return 200-OK with a numeric value.  Safe: pure READ.

Run from the project root:
    python scripts/probe_turbo_api.py
"""

import sys
import os

# Allow running without the venv-activated shell explicitly.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bf4bot.config import load_keys
from bf4bot.reader import FridgeReader

# Turbo-related keys we saw in reader.py's existing log-file code.
# Each one is a CANDIDATE name that might also exist in the REST API.
LOG_KEYS = [
    "tc400actualspd", "tc400drvpower",
    "tc400ovtempelec", "tc400ovtemppum",
    "tc400pumpstatn", "tc400standby",
    "tc400actualspd_2", "tc400drvpower_2",
    "tc400ovtempelec_2", "tc400ovtemppum_2",
    "tc400pumpstatn_2", "tc400standby_2",
]

# Path patterns to try.  "{k}" is replaced with each log key.
PATH_PATTERNS = [
    "bflegacy/double/{k}",     # same pattern cparun uses
    "bflegacy/int/{k}",        # integer variant
    "bflegacy/bool/{k}",       # boolean variant
    "bf/turbo/{k}",            # guesses
    "bf/pumps/{k}",
    "bf/turbopumps/{k}",
    "driver/tc400/{k}",        # driver-namespaced
    "bf/driver/tc400/{k}",
]


def main():
    keys = load_keys()
    reader = FridgeReader(api_key=keys.get("bluefors_api_key", ""))

    hits = []
    tried = 0
    for k in LOG_KEYS:
        for pat in PATH_PATTERNS:
            path = pat.format(k=k)
            tried += 1
            # api_read sometimes raises KeyError on malformed responses
            # from paths that don't exist -- wrap so we can keep probing.
            try:
                r = reader.api_read(path)
            except (KeyError, ValueError, Exception):  # pylint: disable=broad-except
                continue
            status = r.get("status", "?")
            value = r.get("value", "")
            if status != "INVALID" and value not in (None, ""):
                hits.append((path, status, value))
                print(f"  [HIT]  {path}  status={status}  value={value}")
    print(f"\nTried {tried} paths.  {len(hits)} returned real data.")


if __name__ == "__main__":
    main()
