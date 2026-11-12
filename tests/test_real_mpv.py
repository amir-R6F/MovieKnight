"""Optional tests against a real mpv. Excluded by default.

Run them on a machine with mpv installed:

    pytest -m real_mpv
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

MPV = shutil.which("mpv") or shutil.which("mpv.exe")

pytest.importorskip("python_mpv_jsonipc")

pytestmark = [
    pytest.mark.real_mpv,
    pytest.mark.skipif(MPV is None, reason="mpv is not installed on this machine"),
]


async def test_mpv_starts_reports_idle_state_and_closes():
    from player import MpvPlayer

    player = MpvPlayer()
    player.start()
    try:
        assert player.is_running
        assert await player.get_paused() is False
        # Nothing loaded yet: a playback command is remembered, not applied.
        await player.apply_state(5.0, False)
        assert player._pending == (5.0, False)
    finally:
        await asyncio.wait_for(player.close(), timeout=10.0)

    assert player.is_running is False
