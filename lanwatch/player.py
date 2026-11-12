"""mpv wrapper.

LANWatch never streams video. Each peer runs its own local mpv, loaded with
that peer's own copy of the file, and this module is the only thing that talks
to it: over mpv's JSON IPC (a named pipe on Windows).

``python-mpv-jsonipc`` spawns mpv with ``--input-ipc-server`` and ``--idle``,
then exposes commands and properties as Python calls. All of those calls block
on the pipe, so every public method here is a coroutine that runs the blocking
work in a worker thread and keeps the asyncio loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from typing import Any, Protocol, runtime_checkable

import config

log = logging.getLogger(__name__)

# The dependency is optional at import time so the GUI can still start (and
# offer "Open File in mpv") when the package is missing or broken.
try:  # pragma: no cover - depends on the environment
    from python_mpv_jsonipc import MPV, MPVError as _LibError

    _IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover
    MPV = None  # type: ignore[assignment]
    _LibError = Exception  # type: ignore[assignment,misc]
    _IMPORT_ERROR = str(_exc)


class PlayerError(RuntimeError):
    """mpv could not be started, or the IPC connection failed."""


@runtime_checkable
class PlayerLike(Protocol):
    """What the host/client nodes need from a player.

    Kept small on purpose so tests can substitute a fake without any mpv.
    """

    async def get_position(self) -> float: ...

    async def get_duration(self) -> float | None: ...

    async def apply_state(
        self, position: float | None, is_playing: bool, *, force: bool = False
    ) -> None: ...

    async def close(self) -> None: ...


class MpvPlayer:
    """Controls one local mpv process over JSON IPC."""

    def __init__(self, pipe_name: str | None = None, mpv_path: str | None = None) -> None:
        self.pipe_name = pipe_name or config.mpv_pipe_name()
        self.mpv_path = mpv_path if mpv_path is not None else config.find_mpv()
        self._mpv: Any = None
        self._closed = False
        self._media_path: str | None = None
        # State requested before a file was loaded, replayed on load.
        self._pending: tuple[float, bool] | None = None
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Launch mpv and connect to its IPC pipe. Blocks for a moment."""
        if _IMPORT_ERROR is not None:
            raise PlayerError(
                "python-mpv-jsonipc is not available "
                f"({_IMPORT_ERROR}). Run: pip install -r requirements.txt"
            )
        if MPV is None:  # pragma: no cover - defensive
            raise PlayerError("python-mpv-jsonipc is not installed")
        if not self.mpv_path:
            raise PlayerError(
                "mpv was not found. Install mpv, add it to PATH, or set the "
                "LANWATCH_MPV environment variable to mpv.exe."
            )

        try:
            self._mpv = MPV(
                ipc_socket=self.pipe_name,
                start_mpv=True,
                mpv_location=self.mpv_path,
                # Fast failure beats a long retry loop: the GUI explains what
                # to install and the exact-file fallback still works.
                start_retries=2,
                start_retry_delay_ms=300,
                # mpv's stdout/stderr must not keep a pipe open forever.
                discard_output=True,
            )
        except Exception as exc:
            self._mpv = None
            raise PlayerError(f"Could not start mpv: {exc}") from exc

        log.info("mpv started (IPC %s)", self.pipe_name)

    @property
    def is_running(self) -> bool:
        return self._mpv is not None and not self._closed

    @property
    def media_path(self) -> str | None:
        return self._media_path

    async def close(self) -> None:
        """Terminate mpv and the IPC connection. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        mpv, self._mpv = self._mpv, None
        self._media_path = None
        self._pending = None
        if mpv is None:
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(mpv.terminate), timeout=8.0)
        except Exception as exc:  # pragma: no cover - shutdown best effort
            log.warning("Could not shut mpv down cleanly: %s", exc)

    def close_now(self) -> None:
        """Blocking shutdown, for when the asyncio loop is already gone."""
        if self._closed:
            return
        self._closed = True
        mpv, self._mpv = self._mpv, None
        self._media_path = None
        self._pending = None
        if mpv is None:
            return
        try:
            mpv.terminate()
        except Exception:  # pragma: no cover - shutdown best effort
            pass

    # --- blocking primitives (run in a worker thread) ------------------------

    def _guard(self) -> Any:
        if self._mpv is None or self._closed:
            raise PlayerError("mpv is not running")
        return self._mpv

    def _load_blocking(self, path: str) -> None:
        mpv = self._guard()
        mpv.play(path)
        self._media_path = path

    def _position_blocking(self) -> float:
        mpv = self._guard()
        try:
            position = mpv.time_pos
        except _LibError as exc:
            raise PlayerError(f"Could not read time-pos: {exc}") from exc
        return float(position) if isinstance(position, (int, float)) else 0.0

    def _paused_blocking(self) -> bool:
        mpv = self._guard()
        try:
            return bool(mpv.pause)
        except _LibError as exc:
            raise PlayerError(f"Could not read pause state: {exc}") from exc

    def _duration_blocking(self) -> float | None:
        mpv = self._guard()
        try:
            duration = mpv.duration
        except _LibError as exc:
            raise PlayerError(f"Could not read duration: {exc}") from exc
        if isinstance(duration, (int, float)) and duration > 0:
            return float(duration)
        return None

    def _apply_blocking(
        self, position: float | None, is_playing: bool, force: bool
    ) -> None:
        mpv = self._guard()
        try:
            if not is_playing:
                # Pause first so a paused seek cannot overshoot audibly.
                mpv.pause = True

            if position is not None:
                current = mpv.time_pos
                current_value = float(current) if isinstance(current, (int, float)) else 0.0
                if force or abs(current_value - position) > config.SEEK_EPSILON:
                    mpv.command("seek", f"{max(0.0, position):.3f}", "absolute+exact")

            if is_playing:
                mpv.pause = False
        except _LibError as exc:
            raise PlayerError(f"mpv command failed: {exc}") from exc
        except (BrokenPipeError, OSError, TimeoutError) as exc:
            raise PlayerError(f"Lost the connection to mpv: {exc}") from exc

    # --- async API -----------------------------------------------------------

    async def open_file(self, path: str) -> float | None:
        """Load a video file and return its duration (None if unknown yet)."""
        absolute = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(absolute):
            raise PlayerError(f"File not found: {absolute}")

        async with self._lock:
            await asyncio.to_thread(self._load_blocking, absolute)
            duration = await self._wait_for_duration()

            pending, self._pending = self._pending, None
            if pending is not None:
                # A late joiner (or a paused room) had already given us a
                # position before we had anything to play.
                await asyncio.to_thread(self._apply_blocking, pending[0], pending[1], True)
        log.info("Loaded %s (duration %s)", absolute, duration)
        return duration

    async def _wait_for_duration(self, timeout: float = 3.0) -> float | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                duration = await asyncio.to_thread(self._duration_blocking)
            except PlayerError:
                return None
            if duration:
                return duration
            await asyncio.sleep(0.1)
        return None

    async def get_position(self) -> float:
        try:
            return await asyncio.to_thread(self._position_blocking)
        except PlayerError as exc:
            log.debug("Position unavailable: %s", exc)
            return 0.0

    async def get_duration(self) -> float | None:
        try:
            return await asyncio.to_thread(self._duration_blocking)
        except PlayerError as exc:
            log.debug("Duration unavailable: %s", exc)
            return None

    async def get_paused(self) -> bool:
        """True when mpv is paused.

        Optional extra for the nodes: they use it (via ``getattr``) so a
        resync follows what mpv is really doing, not just what we last asked
        for, which matters if the user paused mpv directly.
        """
        try:
            return bool(await asyncio.to_thread(self._paused_blocking))
        except PlayerError as exc:
            log.debug("Pause state unavailable: %s", exc)
            return False

    async def apply_state(
        self, position: float | None, is_playing: bool, *, force: bool = False
    ) -> None:
        """Seek/pause/play to match the room.

        "Stop" arrives here as ``position=0, is_playing=False``: playback is
        paused at the start but the file stays loaded, so the next Play works
        without reopening anything.

        Before any file is loaded the request is remembered and replayed by
        ``open_file``; that is what lets a late joiner open their file after
        the room is already paused at 12:34.
        """
        if self._mpv is None or self._closed:
            raise PlayerError("mpv is not running")

        async with self._lock:
            if self._media_path is None:
                if position is not None:
                    self._pending = (float(position), bool(is_playing))
                return
            await asyncio.to_thread(self._apply_blocking, position, is_playing, force)


def open_in_external_player(path: str, mpv_path: str | None = None) -> None:
    """Open *path* in mpv outside of LANWatch ("Open File in mpv" fallback).

    Used when the IPC pipe could not be established: playback then works, it
    just cannot be synchronised. Falls back to the OS file association.
    """
    absolute = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(absolute):
        raise PlayerError(f"File not found: {absolute}")

    executable = mpv_path or config.find_mpv()
    try:
        if executable:
            subprocess.Popen([executable, absolute], close_fds=True)
            return
        if sys.platform.startswith("win"):
            os.startfile(absolute)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", absolute])
        else:
            subprocess.Popen(["xdg-open", absolute])
    except OSError as exc:
        raise PlayerError(f"Could not open the file: {exc}") from exc
