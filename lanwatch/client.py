"""Client side of LANWatch.

A client connects to the host, measures the clock offset with five NTP-style
rounds, joins, and from then on mirrors every action it is sent. Local actions
are applied immediately and sent to the host, which re-broadcasts them to
everyone (including us -- see MessageTracker for the echo suppression).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any, Callable

import config
import protocol
from clock_sync import ClockSync
from player import PlayerError, PlayerLike
from protocol import MessageTracker, PlaybackState
from websockets.exceptions import ConnectionClosed

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect

    _NEW_CLIENT_API = True
except ImportError:  # pragma: no cover - websockets 12 fallback
    from websockets.client import connect as ws_connect  # type: ignore[no-redef]

    _NEW_CLIENT_API = False

log = logging.getLogger(__name__)

#: Callback into the UI: (event_name, payload).
EventSink = Callable[[str, dict[str, Any]], None]


class ClientNode:
    """A viewer that follows the host."""

    def __init__(
        self,
        *,
        player: PlayerLike | None,
        display_name: str,
        host: str,
        port: int = config.DEFAULT_PORT,
        on_event: EventSink | None = None,
        sender_id: str | None = None,
    ) -> None:
        self.player = player
        self.display_name = display_name.strip()[:32] or "Peer"
        self.host = host.strip() or config.DEFAULT_HOST
        self.port = int(port)
        self.sender_id = sender_id or str(uuid.uuid4())

        # Read the round count at construction time so tests can shorten the
        # handshake without patching the class.
        self.clock = ClockSync(rounds=config.CLOCK_SYNC_ROUNDS)
        self._on_event = on_event
        self._tracker = MessageTracker()
        self._state = PlaybackState()
        self._peers: list[dict[str, Any]] = []
        self._peer_signature: tuple[tuple[str, str, str], ...] | None = None
        self._buffer: list[dict[str, Any]] = []
        self._ws: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._duration: float | None = None
        self._playing = False
        self._connected = False
        self._closing = False
        self._lost = False

        # Countdown before play, and the last seek target so a Play right
        # after a seek counts down from the seek position.
        self._countdown_task: asyncio.Task[Any] | None = None
        self._recent_seek: tuple[float, float] | None = None

    # --- lifecycle -----------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None

    async def connect(self) -> None:
        """Connect, sync the clock, join the room and start mirroring."""
        if self._connected:
            return

        self._closing = False
        self._lost = False
        self._buffer.clear()
        uri = f"ws://{self.host}:{self.port}"
        self._emit("status", {"text": f"Connecting to {uri} ..."})

        kwargs: dict[str, Any] = {
            "open_timeout": config.CONNECT_TIMEOUT,
            "ping_interval": 10.0,
            "ping_timeout": 10.0,
            "close_timeout": 2.0,
            "max_size": 1 << 20,
        }
        if _NEW_CLIENT_API:
            # websockets >= 15 honours HTTP(S)_PROXY from the environment by
            # default; a corporate proxy would swallow LAN traffic.
            kwargs["proxy"] = None

        try:
            connection = await ws_connect(uri, **kwargs)
        except Exception as exc:
            self._emit("status", {"text": f"Could not connect to {uri}"})
            raise ConnectionError(f"{uri}: {exc}") from exc

        self._ws = connection
        try:
            sample = await self.clock.measure(self._ping_round)
            self._emit("sync", {"offset": sample.offset, "rtt": sample.rtt})
            await asyncio.wait_for(self._join(), timeout=config.JOIN_TIMEOUT)
        except BaseException:
            self._ws = None
            with contextlib.suppress(Exception):
                await connection.close()
            raise

        self._connected = True
        self._spawn(self._recv_loop(connection))
        self._spawn(self._tick_loop())
        log.info("Connected to %s:%d", self.host, self.port)
        self._emit(
            "connection",
            {
                "role": "client",
                "connected": True,
                "detail": f"Connected to {self.host}:{self.port}",
            },
        )

    async def disconnect(self) -> None:
        """Leave the room cleanly: stop tasks, close the socket, pause mpv."""
        self._closing = True
        self._connected = False

        await self._cancel_countdown(notify=True)
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        connection, self._ws = self._ws, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

        await self._pause_local()
        self.clock.reset()
        self._state.set(0.0, False)
        self._duration = None
        self._peers = []
        self._peer_signature = None

        self._emit(
            "connection", {"role": "client", "connected": False, "detail": "Disconnected"}
        )
        self._emit("peers", {"peers": []})

    async def stop(self) -> None:
        """Alias used by the GUI's shutdown path."""
        await self.disconnect()

    # --- local actions -------------------------------------------------------

    async def local_action(
        self,
        action: str,
        position: float | None = None,
        *,
        start_in_seconds: float = 0.0,
    ) -> None:
        """Handle a play/pause/seek/stop from this machine's own UI.

        A positive ``start_in_seconds`` on play asks the host (and therefore
        everyone) for a countdown instead of an instant start.
        """
        if action not in protocol.ACTIONS:
            raise ValueError(f"unknown action {action!r}")

        if position is None:
            position = await self._player_position()
        position = max(0.0, protocol.as_float(position))

        # Play while already playing: no seek, no countdown, no message.
        if action == protocol.MSG_PLAY and self._playing:
            return

        if action == protocol.MSG_PLAY and start_in_seconds > 0:
            target = self._countdown_target(position)
            connection = self._ws
            if connection is not None and self._connected:
                # Tracked like any other request, but the host re-broadcasts a
                # countdown under a NEW msg_id, so this one never comes back.
                message = self._tracker.issue(
                    action,
                    self.sender_id,
                    position=target,
                    is_playing=False,
                    start_in_seconds=start_in_seconds,
                )
                try:
                    await connection.send(protocol.encode(message))
                except (ConnectionClosed, OSError) as exc:
                    log.warning("Could not send the countdown play: %s", exc)
                    await self._on_connection_lost()
            await self._begin_countdown(target, start_in_seconds)
            return

        await self._cancel_countdown()

        if action == protocol.MSG_STOP:
            position, playing = 0.0, False
        elif action == protocol.MSG_PLAY:
            playing = True
        elif action == protocol.MSG_PAUSE:
            playing = False
        else:  # seek keeps the current play/pause state
            playing = self._playing
            self._recent_seek = (position, protocol.now())

        connection = self._ws
        if connection is not None and self._connected:
            # Remember the id so the host's echo of this very message is
            # recognised and skipped: we apply it locally right now.
            message = self._tracker.issue(
                action,
                self.sender_id,
                position=position,
                is_playing=playing,
            )
            try:
                await connection.send(protocol.encode(message))
            except (ConnectionClosed, OSError) as exc:
                log.warning("Could not send %s to the host: %s", action, exc)
                await self._on_connection_lost()

        self._state.set(position, playing)
        await self._apply_player(position, playing, force=action in protocol.EXACT_ACTIONS)
        await self._emit_state()

    async def local_resync(self) -> None:
        """Ask the host to pull everyone onto its own current position."""
        connection = self._ws
        if connection is None or not self._connected:
            self._emit(
                "warning", {"text": "Resync needs an active host connection."}
            )
            return

        self._emit("status", {"text": "Resyncing…"})
        try:
            await connection.send(
                protocol.encode(protocol.make_resync_request(self.sender_id))
            )
        except (ConnectionClosed, OSError) as exc:
            log.warning("Could not request a resync: %s", exc)
            await self._on_connection_lost()

    # --- handshake -----------------------------------------------------------

    async def _ping_round(self, t1: float) -> dict[str, Any]:
        """One clock-sync round (runs before the receive loop starts)."""
        connection = self._ws
        if connection is None:
            raise RuntimeError("not connected")

        await connection.send(
            protocol.encode(protocol.make_sync_ping(self.sender_id, t1))
        )
        while True:
            raw = await connection.recv()
            message = protocol.decode(raw)
            if (
                message.get("type") == protocol.MSG_SYNC_PONG
                and abs(protocol.as_float(message.get("t1"), -1.0) - t1) < 1e-6
            ):
                return message
            # Anything else (a heartbeat, say) is handled after the handshake.
            self._buffer.append(message)

    async def _join(self) -> None:
        connection = self._ws
        if connection is None:
            raise RuntimeError("not connected")

        duration = await self._player_duration()
        await connection.send(
            protocol.encode(
                protocol.make_join(self.sender_id, self.display_name, duration)
            )
        )

        welcome: dict[str, Any] | None = None
        while welcome is None:
            if self._buffer:
                message = self._buffer.pop(0)
            else:
                raw = await connection.recv()
                message = protocol.decode(raw)
            if message.get("type") == protocol.MSG_WELCOME:
                welcome = message
            else:
                self._buffer.append(message)

        await self._adopt_welcome(welcome)

    async def _adopt_welcome(self, welcome: dict[str, Any]) -> None:
        """Match the host's state, peer list and duration warnings."""
        raw_state = welcome.get("state")
        state = raw_state if isinstance(raw_state, dict) else {}

        host_duration = state.get("duration")
        if isinstance(host_duration, (int, float)) and host_duration > 0:
            self._duration = float(host_duration)

        position, playing = protocol.resolve_state(state, self.clock.offset)
        self._state.set(position, playing)
        self._set_peers(welcome.get("peers"))
        await self._apply_player(position, playing, force=True)

        warning = welcome.get("warning")
        if warning:
            self._emit("warning", {"text": str(warning)})

        own_duration = await self._player_duration()
        mismatch = self._duration_mismatch(own_duration, host_duration)
        if mismatch:
            self._emit("warning", {"text": mismatch})

        await self._emit_state()

    # --- receive loop --------------------------------------------------------

    async def _recv_loop(self, connection: Any) -> None:
        try:
            buffered, self._buffer = self._buffer, []
            for message in buffered:
                await self._handle(message)

            async for raw in connection:
                try:
                    message = protocol.decode(raw)
                except protocol.ProtocolError as exc:
                    log.warning("Ignoring bad frame from host: %s", exc)
                    continue
                await self._handle(message)
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - keep the GUI alive
            log.exception("Client receive loop failed")
        finally:
            if not self._closing:
                await self._on_connection_lost()

    async def _handle(self, message: dict[str, Any]) -> None:
        msg_type = message.get("type")

        if msg_type == protocol.MSG_HEARTBEAT:
            await self._on_heartbeat(message)
            return

        if msg_type == protocol.MSG_PEER_LEFT:
            left_id = message.get("peer_id") or message.get("sender_id")
            self._remove_peer(left_id)
            return

        if msg_type == protocol.MSG_RESYNC_APPLY:
            # Authoritative and never suppressed: the host created this msg_id,
            # so it cannot collide with whatever request started it.
            await self._cancel_countdown()
            position, playing = protocol.resolve_target(message, self.clock.offset)
            self._state.set(position, playing)
            await self._apply_player(position, playing, force=True)
            self._emit("status", {"text": "Resynced to the host's position"})
            await self._emit_state()
            return

        if msg_type in protocol.ACTIONS:
            if protocol.is_countdown(message):
                # Not echo-suppressed: the host re-broadcast it under a fresh
                # msg_id precisely so the requester runs the overlay too.
                position, remaining = protocol.resolve_countdown(
                    message, self.clock.offset
                )
                await self._begin_countdown(position, remaining)
                return

            if self._tracker.is_echo(message.get("msg_id")):
                # Our own action, already applied on the key press.
                return

            await self._cancel_countdown()
            position, playing = protocol.resolve_target(message, self.clock.offset)
            self._state.set(position, playing)
            await self._apply_player(
                position, playing, force=msg_type in protocol.EXACT_ACTIONS
            )
            await self._emit_state()
            return

        if msg_type in (protocol.MSG_WELCOME, protocol.MSG_SYNC_PONG):
            return

        log.debug("Ignoring message type %r", msg_type)

    async def _on_heartbeat(self, message: dict[str, Any]) -> None:
        """Refresh the peer list and correct slow drift while playing."""
        peers = message.get("peers")
        if isinstance(peers, list):
            self._set_peers(peers)

        if self._countdown_task is not None:
            # A heartbeat must never unpause the room during a countdown; the
            # countdown itself decides when playback starts.
            return

        if message.get("position") is None:
            return

        position, playing = protocol.resolve_target(message, self.clock.offset)
        self._state.set(position, playing)

        if self.player is None or not self._duration:
            # Nothing loaded locally: nothing to correct (apply_state would
            # just remember the position for when a file is opened).
            return

        try:
            current = await self.player.get_position()
            duration = await self.player.get_duration()
        except PlayerError:
            return
        if duration:
            self._duration = duration

        drift = position - current
        if abs(drift) > config.DRIFT_THRESHOLD or playing != self._playing:
            log.debug("Correcting drift of %.3fs", drift)
            await self._apply_player(position, playing)

    async def _on_connection_lost(self) -> None:
        """The host vanished: stop, report it, keep the app usable."""
        if self._lost:
            return
        self._lost = True
        self._connected = False

        await self._cancel_countdown(notify=True)

        connection, self._ws = self._ws, None
        if connection is not None:
            with contextlib.suppress(Exception):
                await connection.close()

        await self._pause_local()
        self._emit(
            "connection",
            {"role": "client", "connected": False, "detail": "Disconnected from host"},
        )
        self._emit(
            "warning",
            {"text": "Host disconnected. Playback paused — press Reconnect to rejoin."},
        )
        log.info("Disconnected from host")

    # --- background loops ----------------------------------------------------

    async def _tick_loop(self) -> None:
        next_beat = protocol.now() + config.HEARTBEAT_INTERVAL
        while True:
            await asyncio.sleep(config.TICK_INTERVAL)
            await self._emit_state()
            if protocol.now() >= next_beat:
                next_beat = protocol.now() + config.HEARTBEAT_INTERVAL
                await self._send_heartbeat()

    async def _send_heartbeat(self) -> None:
        """Tell the host we are still here (state comes back the other way)."""
        connection = self._ws
        if connection is None or not self._connected:
            return
        try:
            await connection.send(
                protocol.encode(protocol.make_heartbeat(self.sender_id))
            )
        except (ConnectionClosed, OSError):
            await self._on_connection_lost()

    async def _emit_state(self) -> None:
        if self.player is None:
            position = self._state.position_at()
        else:
            position = await self._player_position()

        duration = await self._player_duration()
        if duration:
            self._duration = duration

        self._emit(
            "state",
            {
                "position": position,
                "duration": duration or self._duration or 0.0,
                "is_playing": self._playing,
                "offset": self.clock.offset,
                "rtt": self.clock.rtt,
            },
        )

    # --- player helpers ------------------------------------------------------

    async def _player_position(self) -> float:
        if self.player is None:
            return self._state.position_at()
        try:
            return await self.player.get_position()
        except PlayerError:
            return self._state.position_at()

    async def _player_duration(self) -> float | None:
        if self.player is None:
            return None
        try:
            return await self.player.get_duration()
        except PlayerError:
            return None

    async def _apply_player(
        self, position: float | None, playing: bool, *, force: bool = False
    ) -> None:
        self._playing = playing
        if self.player is None:
            return
        try:
            await self.player.apply_state(position, playing, force=force)
        except PlayerError as exc:
            self._emit("error", {"text": f"mpv error: {exc}"})

    async def _pause_local(self) -> None:
        position = self._state.position_at()
        self._state.set(position, False)
        await self._apply_player(position, False)

    # --- countdown before play ----------------------------------------------

    def _countdown_target(self, position: float) -> float:
        """Park where we were just asked to seek, if that was <1s ago."""
        recent = self._recent_seek
        if recent is not None and protocol.now() - recent[1] <= 1.0:
            return recent[0]
        return max(0.0, protocol.as_float(position))

    async def _begin_countdown(self, position: float, seconds: float) -> None:
        """Park on *position*, paused, and start playing after *seconds*."""
        await self._cancel_countdown(notify=False)
        position = max(0.0, protocol.as_float(position))
        seconds = max(0.0, protocol.as_float(seconds))

        if seconds <= 0.0:
            self._state.set(position, True)
            await self._apply_player(position, True, force=True)
            await self._emit_state()
            return

        self._state.set(position, False)
        await self._apply_player(position, False, force=True)
        self._countdown_task = self._spawn(self._run_countdown(position, seconds))
        self._emit("countdown", {"remaining": seconds, "position": position})
        await self._emit_state()

    async def _run_countdown(self, position: float, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._countdown_task = None
        self._state.set(position, True)
        await self._apply_player(None, True, force=False)
        self._emit("countdown_finished", {})
        await self._emit_state()

    async def _cancel_countdown(self, notify: bool = True) -> None:
        """Stop a running countdown; playback stays paused where it is."""
        task = self._countdown_task
        self._countdown_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if notify:
            self._emit("countdown_cancel", {})

    # --- peer bookkeeping ----------------------------------------------------

    def _set_peers(self, raw_peers: Any) -> None:
        if not isinstance(raw_peers, list):
            return
        peers: list[dict[str, Any]] = []
        for entry in raw_peers:
            if not isinstance(entry, dict):
                continue
            info = protocol.PeerInfo.from_dict(entry)
            info.is_self = info.sender_id == self.sender_id
            peers.append(info.to_dict())

        signature = tuple(
            (peer["sender_id"], peer["display_name"], peer["role"]) for peer in peers
        )
        if signature == self._peer_signature:
            return
        self._peer_signature = signature
        self._peers = peers
        self._emit("peers", {"peers": peers})

    def _remove_peer(self, sender_id: Any) -> None:
        if not isinstance(sender_id, str):
            return
        remaining = [
            peer for peer in self._peers if peer.get("sender_id") != sender_id
        ]
        if len(remaining) == len(self._peers):
            return
        self._peers = remaining
        self._peer_signature = tuple(
            (peer["sender_id"], peer["display_name"], peer["role"])
            for peer in remaining
        )
        self._emit("peers", {"peers": remaining})

    def _duration_mismatch(
        self, own: float | None, host: Any
    ) -> str | None:
        """Warn when our file length differs from the host's by more than 1s."""
        if not own or not isinstance(host, (int, float)) or host <= 0:
            return None
        difference = abs(float(host) - own)
        if difference <= config.DURATION_MISMATCH_TOLERANCE:
            return None
        return (
            f"Video duration differs by {difference:.1f}s "
            f"(host {float(host):.0f}s, yours {own:.0f}s) — "
            "your video may be a different version."
        )

    # --- plumbing ------------------------------------------------------------

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                log.debug("Background task ended: %s", error)

        task.add_done_callback(_done)
        return task

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # pragma: no cover - a UI bug must not kill the client
            log.exception("Event handler raised for %s", kind)
