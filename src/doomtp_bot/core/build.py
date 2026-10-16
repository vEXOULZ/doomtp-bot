"""What this bot is: its version, the tag or branch and commit its image was built from, and how long it has
been up. `/api/v1/stats` and `$bot` read it.

CI writes `ref` and `commit` into `build-info/` before it builds the image (ci.yml), and the Dockerfile copies
the folder to `/app/build-info`. A checkout has no such files, so a bot run from one reports neither.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from doomtp_bot import __version__

BUILD_INFO_DIR = Path(os.environ.get("BUILD_INFO_DIR", "/app/build-info"))


def _read(name: str) -> str | None:
    try:
        text = (BUILD_INFO_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


@dataclass(frozen=True, slots=True)
class BuildInfo:
    version: str
    #: The tag the image was built from (`v0.12.0`), or its branch (`dev`, `main`) when it isn't a release.
    release: str | None
    #: The full commit SHA.
    commit: str | None

    @property
    def short_commit(self) -> str | None:
        return self.commit[:7] if self.commit else None


BUILD = BuildInfo(version=__version__, release=_read("ref"), commit=_read("commit"))
#: When this process started, unix seconds.
STARTED_AT = time.time()


def uptime_s(now: float | None = None) -> int:
    return max(0, int((time.time() if now is None else now) - STARTED_AT))
