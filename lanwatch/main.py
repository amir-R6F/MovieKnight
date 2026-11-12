"""LANWatch entry point.

Starts three things and then gets out of the way:

1. an asyncio event loop, on its own thread, for WebSocket and mpv work;
2. a local mpv process controlled over JSON IPC;
3. the CustomTkinter window (on the main thread).

Closing the window shuts everything down in the reverse order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading

import config
from gui import run_gui
from player import MpvPlayer, PlayerError

log = logging.getLogger("lanwatch")


def _enable_dpi_awareness() -> None:
    """Ask Windows for crisp rendering on high-DPI screens (no admin needed)."""
    if not config.is_windows():
        return
    try:
        import ctypes
    except ImportError:  # pragma: no cover
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        # Older Windows 10 builds only have the user32 entry point.
        with contextlib.suppress(Exception):
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Own the loop for its whole life, then tear it down cleanly."""
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        with contextlib.suppress(Exception):
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


def _start_player() -> tuple[MpvPlayer | None, str | None]:
    """Launch mpv. A failure is not fatal: the GUI still opens."""
    player = MpvPlayer()
    try:
        player.start()
    except PlayerError as exc:
        log.warning("mpv is not available: %s", exc)
        return None, str(exc)
    return player, None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log.info("%s %s starting", config.APP_NAME, config.APP_VERSION)
    _enable_dpi_awareness()

    loop = asyncio.new_event_loop()
    thread = threading.Thread(
        target=_run_loop, args=(loop,), name="lanwatch-async", daemon=True
    )
    thread.start()

    player, player_error = _start_player()

    exit_code = 0
    try:
        run_gui(loop, player, player_error)
    except Exception:
        log.exception("The GUI stopped unexpectedly")
        exit_code = 1
    finally:
        if loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        if player is not None:
            # Normally a no-op: the window's close handler already did this.
            player.close_now()
        log.info("LANWatch stopped")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
