"""Normalise a Windows task return code. Its own module on purpose.

Both server.py (rendering) and console_ingest.py (writing) need this. Importing it from server.py
would close a cycle the moment server.py imports the store, so it lives alone and both import it.

TWO RULES, both learned from measurement rather than reasoning:

1. THE EVENT LOG WRAPS WIN32 CODES AS HRESULTS. A task's LastTaskResult reads 0x2, and event 201 in
   the Operational log reports the same outcome as 2147942402 (0x80070000 | 2). Comparing a task's
   declared ok_codes -- small integers like 2, 3, 4 -- against the wrapped form never matches, so
   every task that encodes a verdict in its exit code would read as 0% success while looking
   entirely plausible.

2. MISSING IS NOT ZERO. Event 100 (task started) carries no return code at all. Returning 0 for it
   writes a fabricated SUCCESS, inflating the success rate by one row per start. None means "this
   event has no return code" and the caller must skip it, not count it.
"""
from __future__ import annotations

E_ACCESSDENIED_FACILITY = 0x80070000


def norm_rc(v):
    """Return an int, or None when there is no return code to speak of."""
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        n = int(str(v), 0)
    except (TypeError, ValueError):
        return None
    if n < 0:
        n += 1 << 32                      # the signed-int32 spelling of the same value
    if E_ACCESSDENIED_FACILITY <= n <= 0x8007FFFF:
        return n & 0xFFFF
    return n
