"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from doomtp_bot.storage.db import Databases


@pytest.fixture
async def dbs(tmp_path: Path) -> AsyncIterator[Databases]:
    """Freshly migrated bot.db and chatlog.db in a temp dir."""
    databases = await Databases.open(tmp_path / "bot.db", tmp_path / "chatlog.db")
    try:
        yield databases
    finally:
        await databases.close()
