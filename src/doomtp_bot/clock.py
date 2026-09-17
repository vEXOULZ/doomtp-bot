"""Wall-clock helper shared by storage writers and adapters."""

from __future__ import annotations

import time


def now_ms() -> int:
    """Milliseconds since the Unix epoch (the unit every stored timestamp uses)."""
    return int(time.time() * 1000)
