"""Unit tests for the mpv wrapper, using a fake mpv object instead of mpv."""

from __future__ import annotations

import pytest

from helpers import FakePlayer
from player import MpvPlayer


class FakeMpv:
    """Just enough of python-mpv-jsonipc's surface for MpvPlayer."""

    def __init__(self) -> None:
        self.pause = True
        self.time_pos = 0.0
        self.duration: float | None = None
        self.commands: list[tuple] = []
        self.terminated = False

    def play(self, path: str) -> None:
        self.duration = 100.0
        self.time_pos = 0.0
        self.pause = True

    def command(self, name: str, *args) -> None:
        self.commands.append((name, *args))
        if name == "seek":
            self.time_pos = float(args[0])

    def terminate(self) -> None:
        self.terminated = True


def _player_with_fake() -> tuple[MpvPlayer, FakeMpv]:
    player = MpvPlayer(pipe_name="lanwatch_test")
    fake = FakeMpv()
    player._mpv = fake
    return player, fake


def _video_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not a real video")
    return path


# --- pending state -----------------------------------------------------------


async def test_state_before_a_file_is_loaded_is_remembered():
    player, fake = _player_with_fake()

    await player.apply_state(12.0, False)

    assert player._pending == (12.0, False)
    assert fake.commands == [], "mpv must not be touched before a file is loaded"


async def test_open_file_applies_the_pending_room_state(tmp_path):
    player, fake = _player_with_fake()
    await player.apply_state(12.0, False)

    duration = await player.open_file(str(_video_file(tmp_path)))

    assert duration == 100.0
    assert fake.time_pos == pytest.approx(12.0)
    assert fake.pause is True


async def test_pause_before_load_is_applied_on_open(tmp_path):
    player, fake = _player_with_fake()
    await player.apply_state(30.0, False)

    await player.open_file(str(_video_file(tmp_path)))

    assert fake.pause is True
    assert fake.time_pos == pytest.approx(30.0)


async def test_play_after_pause_with_zero_countdown_unpauses_immediately(tmp_path):
    """start_in_seconds=0 never reaches the player: play is applied at once."""
    player, fake = _player_with_fake()
    await player.open_file(str(_video_file(tmp_path)))

    await player.apply_state(30.0, False)
    assert fake.pause is True

    fake.commands.clear()
    await player.apply_state(30.0, True)

    assert fake.pause is False
    assert fake.time_pos == pytest.approx(30.0)
    assert all(command[0] == "seek" for command in fake.commands)


# --- command shape -----------------------------------------------------------


async def test_pause_seeks_with_absolute_exact(tmp_path):
    player, fake = _player_with_fake()
    await player.open_file(str(_video_file(tmp_path)))
    fake.commands.clear()

    await player.apply_state(42.5, False, force=True)

    assert ("seek", "42.500", "absolute+exact") in fake.commands
    assert fake.pause is True


async def test_small_differences_do_not_cause_micro_seeks(tmp_path):
    player, fake = _player_with_fake()
    await player.open_file(str(_video_file(tmp_path)))
    await player.apply_state(50.0, False)
    fake.time_pos = 50.02
    fake.commands.clear()

    await player.apply_state(50.0, False)

    assert fake.commands == []


async def test_stop_keeps_the_file_and_parks_at_zero(tmp_path):
    player, fake = _player_with_fake()
    await player.open_file(str(_video_file(tmp_path)))
    await player.apply_state(50.0, True)

    await player.apply_state(0.0, False, force=True)

    assert fake.time_pos == pytest.approx(0.0)
    assert fake.pause is True
    assert player.media_path is not None


# --- reads -------------------------------------------------------------------


async def test_get_position_and_duration_return_current_values(tmp_path):
    player, fake = _player_with_fake()
    await player.open_file(str(_video_file(tmp_path)))
    fake.time_pos = 77.5

    assert await player.get_position() == pytest.approx(77.5)
    assert await player.get_duration() == pytest.approx(100.0)


async def test_get_paused_reports_mpv(tmp_path):
    player, fake = _player_with_fake()
    await player.open_file(str(_video_file(tmp_path)))
    await player.apply_state(1.0, True)

    assert await player.get_paused() is False

    fake.pause = True
    assert await player.get_paused() is True


async def test_reading_without_mpv_degrades_to_zero():
    player = MpvPlayer(pipe_name="lanwatch_test")

    assert await player.get_position() == 0.0
    assert await player.get_duration() is None


# --- shutdown ----------------------------------------------------------------


async def test_close_terminates_mpv_once():
    player, fake = _player_with_fake()

    await player.close()
    await player.close()

    assert fake.terminated is True
    assert player.is_running is False


async def test_apply_after_close_raises():
    player, _ = _player_with_fake()
    await player.close()

    with pytest.raises(Exception):
        await player.apply_state(1.0, True)


# --- the shared FakePlayer used by the integration tests ---------------------


async def test_fake_player_pause_then_play_with_zero_countdown_unpauses():
    player = FakePlayer()

    await player.apply_state(30.0, False)
    assert player.paused is True and player.playing is False

    # What the nodes do for start_in_seconds=0: play right now.
    await player.apply_state(30.0, True)

    assert player.paused is False
    assert player.playing is True
    assert player.position == pytest.approx(30.0)


async def test_fake_player_reports_its_values():
    player = FakePlayer(duration=42.0)
    player.position = 7.5

    assert await player.get_position() == pytest.approx(7.5)
    assert await player.get_duration() == pytest.approx(42.0)
    assert await player.get_paused() is True
