from pathlib import Path

import pytest

from doomtp_bot.core.instance_lock import InstanceLock, InstanceLockError


def test_second_lock_is_refused_until_released(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    first = InstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(InstanceLockError):
            InstanceLock(path).acquire()
    finally:
        first.release()
    with InstanceLock(path):
        pass
