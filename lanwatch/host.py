"""Host side of LANWatch: a small WebSocket server that owns the room state.

The host is the hub. It is the only peer that broadcasts, it stamps every
action with its own monotonic clock (``host_time``), and it answers late
joiners with a ``welcome`` carrying the current playback state.

The host is also a normal viewer: its own player follows every command it
accepts, exactly like a client's does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import config
import protocol
from clock_sync import answer_ping
from discovery import ServiceAdvertiser
from player import PlayerError, PlayerLike
from protocol import PlaybackState
from websockets.exceptions import ConnectionClosed

try:  # websockets >= 13
    from websockets.asyncio.server import serve as ws_serve
except ImportError:  # pragma: no cover - websockets 12 fallback
    from websockets.server import serve as ws_serve  # type: ignore[no-redef]

log = logging.getLogger(__name__)

#: Callback into the UI: (event_name, payload).
EventSink = Callable[[str, dict[str, Any]], None]


@dataclass
class _Peer:
    """A connected client, as the host sees it."""

    sender_id: str
    display_name: str
    connection: Any
    video_duration: float | None = None
    last_seen: float = field(default_factory=protocol.now)


class HostNode:
    """Runs the WebSocket server and keeps the authoritative playback state."""

    def __init__(
        self,
        *,
        player: PlayerLike | None,
        display_name: str,
        port: int = config.DEFAULT_PORT,
        on_event: EventSink | None = None,
        sender_id: str | None = None,
        advertise: bool | None = None,
    ) -> None:
        self.player = player
        self.display_name = display_name.strip()[:32] or "Host"
        self.port = int(port)
        self.sender_id = sender_id or str(uuid.uuid4())

        self._on_event = on_event
        self._clients: dict[str, _Peer] = {}
        self._state = PlaybackState()
        self._duration: float | None = None
        self._server: Any = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()
        self._stopping = False
        self._peer_signature: tuple[tuple[str, str, str], ...] | None = None

        # mDNS is a convenience; tests and offline machines can turn it off.
        self._advertise = config.MDNS_ENABLED if advertise is None else bool(advertise)
        self._advertiser: ServiceAdvertiser | None = None

        # Countdown before play (see gui.overlay.CountdownOverlay).
        self._countdown_task: asyncio.Task[Any] | None = None
        # Last seek we asked for, so a quick Play can count down from there.
        self._recent_seek: tuple[float, float] | None = None

    # --- lifecycle -----------------------------------------------------------

    @property
    def is_hosting(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        """Bind the WebSocket server and start the periodic loops.

        Raises OSError when the port is already in use.
        """
        if self._server is not None:
            return

        # Bind on all interfaces so other machines on the LAN can reach us.
        self._server = await ws_serve(
            self._handle_connection,
            "0.0.0.0",
            self.port,
            ping_interval=10.0,
            ping_timeout=10.0,
            close_timeout=2.0,
            max_size=1 << 20,
        )
        # Port 0 asks the OS for a free port (the tests use that). Advertise
        # the port it actually picked, not the 0 we asked for.
        if self.port == 0:
            sockets = getattr(self._server, "sockets", None) or []
            if sockets:
                self.port = int(sockets[0].getsockname()[1])
        self._stopping = False
        self._spawn(self._tick_loop())
        if self._advertise:
            await self._start_advertising()
        log.info("Hosting on port %d as %s", self.port, self.display_name)
        self._emit(
            "connection",
            {
                "role": "host",
                "connected": True,
                "detail": f"Hosting on port {self.port}",
            },
        )
        self._emit_peers()
        await self._emit_state()

    async def stop(self) -> None:
        """Close every connection and stop the background loops."""
        if self._stopping:
            return
        self._stopping = True

        await self._cancel_countdown(notify=True)
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        advertiser, self._advertiser = self._advertiser, None
        if advertiser is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(advertiser.stop)

        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(Exception):
                server.close(close_connections=True)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=3.0)

        self._clients.clear()
        self._emit("peers", {"peers": [self._self_entry()]})
        self._peer_signature = None
        self._emit(
            "connection",
            {"role": "host", "connected": False, "detail": "Stopped hosting"},
        )
        log.info("Host stopped")

    # --- local playback ------------------------------------------------------

    async def local_action(
        self,
        action: str,
        position: float | None = None,
        *,
        start_in_seconds: float = 0.0,
    ) -> None:
        """Handle a play/pause/seek/stop from this machine's own UI.

        ``start_in_seconds`` is only meaningful for play: when it is positive
        the room counts down 3-2-1 (everyone runs it, this peer included)
        instead of starting instantly.
        """
        if action not in protocol.ACTIONS:
            raise ValueError(f"unknown action {action!r}")

        if position is None:
            position = await self._player_position()
        position = max(0.0, protocol.as_float(position))

        # Play while already playing: no seek, no countdown, no message.
        if action == protocol.MSG_PLAY and self._state.is_playing:
            return

        if action == protocol.MSG_PLAY and start_in_seconds > 0:
            target = self._countdown_target(position)
            message = protocol.make_action(
                action,
                self.sender_id,
                position=target,
                is_playing=False,
                host_time=protocol.now(),
                start_in_seconds=start_in_seconds,
            )
            async with self._lock:
                self._state.set(target, False)
                await self._broadcast(message)
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
            playing = self._state.is_playing
            self._recent_seek = (position, protocol.now())

        message = protocol.make_action(
            action,
            self.sender_id,
            position=position,
            is_playing=playing,
            host_time=protocol.now(),
        )
        async with self._lock:
            self._state.set(position, playing)
            await self._broadcast(message)

        await self._apply_local(position, playing, force=action in protocol.EXACT_ACTIONS)
        await self._emit_state()

    async def local_resync(self) -> None:
        """Make every peer follow this host's current position and pause state.

        This is the authoritative direction of a resync: whether the user
        clicked here or a client asked over the wire, the answer is the same
        broadcast built from the host's own player.
        """
        await self._cancel_countdown()

        position = max(0.0, await self._player_position())
        paused = await self._player_paused()
        playing = self._state.is_playing if paused is None else not paused

        message = protocol.make_resync_apply(
            self.sender_id, position=position, is_playing=playing
        )
        async with self._lock:
            self._state.set(position, playing)
            await self._broadcast(message)

        await self._apply_local(position, playing, force=True)
        self._emit("status", {"text": "Resynced everyone to the host's position"})
        await self._emit_state()

    # --- server --------------------------------------------------------------

    async def _await_join(self, connection: Any) -> _Peer:
        """Wait for the join, answering clock-sync pings that arrive first.

        A client measures the clock offset before it joins (that is how it
        knows where to place the welcome state), so sync_ping is normally the
        first thing we see on a connection.
        """
        while True:
            raw = await asyncio.wait_for(connection.recv(), timeout=config.JOIN_TIMEOUT)
            message = protocol.decode(raw)
            msg_type = message.get("type")

            if msg_type == protocol.MSG_SYNC_PING:
                await connection.send(
                    protocol.encode(answer_ping(self.sender_id, message))
                )
                continue

            if msg_type != protocol.MSG_JOIN:
                raise protocol.ProtocolError("first message must be a join")
            break

        sender_id = message.get("sender_id")
        if not isinstance(sender_id, str) or not sender_id:
            raise protocol.ProtocolError("join is missing sender_id")
        if sender_id == self.sender_id:
            raise protocol.ProtocolError("sender_id collides with the host")

        duration = message.get("video_duration")
        peer = _Peer(
            sender_id=sender_id,
            display_name=str(message.get("display_name") or "Peer")[:32],
            connection=connection,
            video_duration=protocol.as_float(duration) if duration is not None else None,
        )

        # A peer that reconnects with a fresh socket replaces its old one.
        previous = self._clients.get(peer.sender_id)
        if previous is not None:
            self._clients.pop(peer.sender_id, None)
            with contextlib.suppress(Exception):
                await previous.connection.close()

        self._clients[peer.sender_id] = peer

        warning = self._duration_warning(peer.video_duration)
        welcome = protocol.make_welcome(
            self.sender_id,
            state=self._welcome_state(),
            peers=self._peer_payload(),
            warning=warning,
        )
        await connection.send(protocol.encode(welcome))

        self._emit_peers()
        self._emit(
            "status",
            {"text": f"{peer.display_name} joined — {len(self._clients)} peer(s) connected"},
        )
        if warning:
            self._emit("warning", {"text": warning})
        # Push the new peer list to the other clients right away instead of
        # making them wait for the next scheduled heartbeat.
        await self._broadcast_heartbeat()
        return peer

    async def _handle_connection(self, connection: Any) -> None:
        """One client connection: handshake, then the message loop."""
        peer: _Peer | None = None
        try:
            peer = await self._await_join(connection)

            async for raw in connection:
                try:
                    incoming = protocol.decode(raw)
                except protocol.ProtocolError as exc:
                    log.warning("Ignoring bad frame from %s: %s", peer.display_name, exc)
                    continue
                await self._handle_message(peer, incoming)

        except asyncio.TimeoutError:
            log.info("Connection opened but no join arrived in time")
        except protocol.ProtocolError as exc:
            log.warning("Rejected a connection: %s", exc)
            with contextlib.suppress(Exception):
                await connection.close(code=1008, reason="invalid join")
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - never take the server down
            log.exception("Unexpected error in the connection handler")
        finally:
            if peer is not None and self._clients.get(peer.sender_id) is peer:
                self._clients.pop(peer.sender_id, None)
                await self._broadcast(protocol.make_peer_left(peer.sender_id))
                self._emit_peers()
                self._emit(
                    "status",
                    {"text": f"{peer.display_name} left — {len(self._clients)} peer(s) connected"},
                )
                with contextlib.suppress(Exception):
                    await self._broadcast_heartbeat()

    async def _handle_message(self, peer: _Peer, message: dict[str, Any]) -> None:
        msg_type = message.get("type")

        if msg_type == protocol.MSG_SYNC_PING:
            # Answer immediately: t2/t3 are stamped as the reply is built.
            await peer.connection.send(
                protocol.encode(answer_ping(self.sender_id, message))
            )
            return

        if msg_type == protocol.MSG_HEARTBEAT:
            peer.last_seen = protocol.now()
            return

        if msg_type == protocol.MSG_RESYNC_REQUEST:
            # Any peer may ask; the answer always comes from our own player.
            await self.local_resync()
            return

        if msg_type in protocol.ACTIONS:
            await self._apply_remote_action(peer, message)
            return

        log.debug("Ignoring %r from %s", msg_type, peer.display_name)

    async def _apply_remote_action(
        self, peer: _Peer, message: dict[str, Any]
    ) -> None:
        """Adopt a client's action, re-broadcast it and apply it locally."""
        action = message["type"]
        position = max(0.0, protocol.as_float(message.get("position")))
        start_in_seconds = protocol.as_float(message.get("start_in_seconds"))
        countdown = action == protocol.MSG_PLAY and start_in_seconds > 0.0

        if action == protocol.MSG_STOP:
            position, playing = 0.0, False
        elif action == protocol.MSG_PLAY:
            playing = False if countdown else True
        elif action == protocol.MSG_PAUSE:
            playing = False
        else:  # seek
            playing = bool(message.get("is_playing", self._state.is_playing))

        # A countdown play gets a fresh msg_id from the host: its originator
        # must run the overlay too, so reusing the original id (which that peer
        # tracks) would make it suppress its own countdown.
        outgoing = protocol.make_action(
            action,
            peer.sender_id,
            position=position,
            is_playing=playing,
            msg_id=None if countdown else message.get("msg_id"),
            host_time=protocol.now(),
            start_in_seconds=start_in_seconds if countdown else None,
        )

        async with self._lock:
            self._state.set(position, playing)
            await self._broadcast(outgoing)

        if countdown:
            await self._begin_countdown(position, start_in_seconds)
            return

        await self._cancel_countdown()
        await self._apply_local(position, playing, force=action in protocol.EXACT_ACTIONS)
        await self._emit_state()

    # --- broadcasts ----------------------------------------------------------

    async def _broadcast(self, message: dict[str, Any]) -> None:
        """Send to every connected client, dropping the ones that fail."""
        if not self._clients:
            return
        data = protocol.encode(message)
        peers = list(self._clients.values())
        results = await asyncio.gather(
            *(self._send_to(peer, data) for peer in peers), return_exceptions=True
        )
        dead = [result for result in results if isinstance(result, str)]
        if dead:
            for sender_id in dead:
                self._clients.pop(sender_id, None)
            self._emit_peers()

    async def _send_to(self, peer: _Peer, data: str) -> str | None:
        """Send one frame; returns the sender_id when it failed."""
        try:
            await asyncio.wait_for(peer.connection.send(data), timeout=2.0)
        except (ConnectionClosed, OSError, asyncio.TimeoutError):
            return peer.sender_id
        return None

    async def _broadcast_heartbeat(self) -> None:
        position, duration, playing = await self._player_snapshot()
        if duration:
            self._duration = duration
        await self._broadcast(
            protocol.make_heartbeat(
                self.sender_id,
                position=position,
                is_playing=playing,
                peers=self._peer_payload(),
                host_time=protocol.now(),
            )
        )

    # --- background loops ----------------------------------------------------

    async def _tick_loop(self) -> None:
        """Refresh our own player view and periodically re-broadcast state."""
        next_beat = protocol.now() + config.HEARTBEAT_INTERVAL
        while True:
            await asyncio.sleep(config.TICK_INTERVAL)
            await self._drop_stale_peers()
            await self._emit_state()
            if protocol.now() >= next_beat:
                next_beat = protocol.now() + config.HEARTBEAT_INTERVAL
                await self._broadcast_heartbeat()

    async def _drop_stale_peers(self) -> None:
        """Remove clients that stopped sending heartbeats.

        Their socket may be half-open (a laptop lid closed, a Wi-Fi drop), so
        the peer list would otherwise show a ghost until the TCP keepalive
        gives up minutes later.
        """
        if not self._clients:
            return

        cutoff = protocol.now() - config.PEER_TIMEOUT
        stale = [peer for peer in self._clients.values() if peer.last_seen < cutoff]
        if not stale:
            return

        for peer in stale:
            if self._clients.get(peer.sender_id) is not peer:
                continue
            self._clients.pop(peer.sender_id, None)
            log.info("Dropping silent peer %s", peer.display_name)
            with contextlib.suppress(Exception):
                await peer.connection.close()
            await self._broadcast(protocol.make_peer_left(peer.sender_id))
            self._emit(
                "status",
                {
                    "text": f"{peer.display_name} timed out — "
                    f"{len(self._clients)} peer(s) connected"
                },
            )
        self._emit_peers()
        await self._broadcast_heartbeat()

    async def _emit_state(self) -> None:
        position, duration, playing = await self._player_snapshot()
        if duration:
            self._duration = duration
        self._emit(
            "state",
            {
                "position": position,
                "duration": duration or self._duration or 0.0,
                "is_playing": playing,
                "offset": None,
                "rtt": None,
            },
        )

    # --- player helpers ------------------------------------------------------

    async def _player_snapshot(self) -> tuple[float, float | None, bool]:
        """(position, duration, is_playing) for heartbeats and the UI.

        Once a file is loaded, our own mpv is the master clock: its actual
        position is what everyone should be watching. Before that, the
        command-derived state is all we have.
        """
        playing = self._state.is_playing
        if self.player is None:
            return self._state.position_at(), self._duration, playing
        try:
            duration = await self.player.get_duration()
            if not duration:
                return self._state.position_at(), self._duration, playing
            position = await self.player.get_position()
        except PlayerError:
            return self._state.position_at(), self._duration, playing
        return position, duration, playing

    async def _player_position(self) -> float:
        if self.player is None:
            return self._state.position_at()
        try:
            return await self.player.get_position()
        except PlayerError:
            return self._state.position_at()

    async def _apply_local(self, position: float, playing: bool, *, force: bool) -> None:
        if self.player is None:
            return
        try:
            await self.player.apply_state(position, playing, force=force)
        except PlayerError as exc:
            self._emit("error", {"text": f"mpv error: {exc}"})

    async def _player_paused(self) -> bool | None:
        """mpv's real pause state, or None when we cannot ask."""
        if self.player is None:
            return None
        getter = getattr(self.player, "get_paused", None)
        if getter is None:
            return None
        try:
            return bool(await getter())
        except PlayerError:
            return None

    # --- countdown before play ----------------------------------------------

    def _countdown_target(self, position: float) -> float:
        """Where the countdown should park.

        A seek fired moments ago wins over the GUI's last polled position, so
        "seek, then Play" counts down from the seek target rather than from
        where playback used to be.
        """
        recent = self._recent_seek
        if recent is not None and protocol.now() - recent[1] <= 1.0:
            return recent[0]
        return max(0.0, protocol.as_float(position))

    async def _begin_countdown(self, position: float, seconds: float) -> None:
        """Park on *position*, paused, then start playing after *seconds*."""
        await self._cancel_countdown(notify=False)
        position = max(0.0, protocol.as_float(position))
        seconds = max(0.0, protocol.as_float(seconds))

        if seconds <= 0.0:
            self._state.set(position, True)
            await self._apply_local(position, True, force=True)
            await self._emit_state()
            return

        self._state.set(position, False)
        await self._apply_local(position, False, force=True)
        self._countdown_task = self._spawn(self._run_countdown(position, seconds))
        self._emit("countdown", {"remaining": seconds, "position": position})
        await self._emit_state()

    async def _run_countdown(self, position: float, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._countdown_task = None
        self._state.set(position, True)
        await self._apply_local(None, True, force=False)
        self._emit("countdown_finished", {})
        await self._emit_state()

    async def _cancel_countdown(self, notify: bool = True) -> None:
        """Stop a running countdown; the peer stays paused where it is."""
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

    # --- mDNS ---------------------------------------------------------------

    async def _start_advertising(self) -> None:
        """Publish this host over mDNS. A failure only costs the convenience."""
        advertiser = ServiceAdvertiser(self.port, self.display_name)
        self._advertiser = advertiser
        try:
            started = await asyncio.to_thread(advertiser.start)
        except Exception as exc:  # pragma: no cover - environment dependent
            log.warning("mDNS advertising failed: %s", exc)
            started = False
        if started:
            return

        self._advertiser = None
        self._emit(
            "status",
            {"text": "mDNS discovery is unavailable — share your IP manually."},
        )

    # --- peer bookkeeping ----------------------------------------------------

    def _self_entry(self) -> dict[str, Any]:
        return {
            "sender_id": self.sender_id,
            "display_name": self.display_name,
            "role": "host",
            "video_duration": self._duration,
            "self": True,
        }

    def _peer_payload(self) -> list[dict[str, Any]]:
        peers = [self._self_entry()]
        for peer in self._clients.values():
            peers.append(
                {
                    "sender_id": peer.sender_id,
                    "display_name": peer.display_name,
                    "role": "client",
                    "video_duration": peer.video_duration,
                    "self": False,
                }
            )
        return peers

    def _emit_peers(self) -> None:
        peers = self._peer_payload()
        signature = tuple(
            (peer["sender_id"], peer["display_name"], peer["role"]) for peer in peers
        )
        if signature == self._peer_signature:
            return
        self._peer_signature = signature
        self._emit("peers", {"peers": peers})

    def _welcome_state(self) -> dict[str, Any]:
        state = self._state.snapshot()
        state["duration"] = self._duration
        return state

    def _duration_warning(self, client_duration: float | None) -> str | None:
        """Compare the joiner's video length with ours (>1 s means a mismatch)."""
        if not client_duration or not self._duration:
            return None
        difference = abs(client_duration - self._duration)
        if difference <= config.DURATION_MISMATCH_TOLERANCE:
            return None
        return (
            f"Video duration differs by {difference:.1f}s "
            f"(host {self._duration:.0f}s, joiner {client_duration:.0f}s) — "
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
        except Exception:  # pragma: no cover - a UI bug must not kill the host
            log.exception("Event handler raised for %s", kind)
