"""Test doubles shared by the LANWatch suite (no mpv, no real media).

Everything here is deliberately synchronous-friendly: the fake player has the
same async surface as ``MpvPlayer``, and the raw peer speaks the real protocol
over a real WebSocket so the tests exercise the wire format too.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

import protocol
from client import ClientNode
from websockets.asyncio.client import connect as ws_connect


class FakePlayer:
    """Same async surface as MpvPlayer, but everything is in memory."""

    def __init__(self, duration: float | None = 600.0) -> None:
        self.position = 0.0
        self.playing = False
        self.paused = True
        self.duration = duration
        self.applies: list[tuple[float | None, bool, bool]] = []
        self.closed = False

    async def get_position(self) -> float:
        return self.position

    async def get_duration(self) -> float | None:
        return self.duration

    async def get_paused(self) -> bool:
        return self.paused

    async def apply_state(
        self, position: float | None, is_playing: bool, *, force: bool = False
    ) -> None:
        self.applies.append((position, bool(is_playing), force))
        if position is not None:
            self.position = float(position)
        self.playing = bool(is_playing)
        self.paused = not is_playing

    async def close(self) -> None:
        self.closed = True


class EventCollector:
    """Callable that records the (kind, payload) events a node emits."""

    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, kind: str, payload: dict[str, Any]) -> None:
        self.items.append((kind, dict(payload)))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.items if event == kind]

    def kinds(self) -> list[str]:
        return [event for event, _ in self.items]

    def has(self, kind: str) -> bool:
        return any(event == kind for event, _ in self.items)

    def last(self, kind: str) -> dict[str, Any] | None:
        found = self.of(kind)
        return found[-1] if found else None

    def clear(self) -> None:
        self.items.clear()


async def wait_until(
    predicate, timeout: float = 3.0, interval: float = 0.02
) -> bool:
    """Poll *predicate* until it is true or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


async def start_peer(
    port: int,
    name: str = "Peer",
    *,
    duration: float | None = 600.0,
    host: str = "127.0.0.1",
) -> tuple[ClientNode, FakePlayer, EventCollector]:
    """A connected ClientNode with a fake player and its event log."""
    player = FakePlayer(duration)
    events = EventCollector()
    node = ClientNode(
        player=player,
        display_name=name,
        host=host,
        port=port,
        on_event=events,
    )
    await node.connect()
    return node, player, events


class RawPeer:
    """A bare WebSocket peer, used to inspect exactly what the host sends."""

    def __init__(self, connection: Any, sender_id: str) -> None:
        self.connection = connection
        self.sender_id = sender_id

    @classmethod
    async def join(
        cls, port: int, name: str = "Raw", duration: float | None = 600.0
    ) -> "RawPeer":
        connection = await ws_connect(f"ws://127.0.0.1:{port}", proxy=None)
        sender_id = f"raw-{uuid.uuid4().hex[:8]}"
        peer = cls(connection, sender_id)
        await connection.send(
            protocol.encode(protocol.make_join(sender_id, name, duration))
        )
        await asyncio.wait_for(connection.recv(), 3.0)  # welcome
        return peer

    async def send(self, message: dict[str, Any]) -> None:
        await self.connection.send(protocol.encode(message))

    async def send_raw(self, raw: str) -> None:
        await self.connection.send(raw)

    async def next(self, message_type: str, timeout: float = 3.0) -> dict[str, Any]:
        """The next message of *message_type*, skipping the rest."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {message_type} message within {timeout}s")
            raw = await asyncio.wait_for(self.connection.recv(), max(0.05, remaining))
            message = protocol.decode(raw)
            if message.get("type") == message_type:
                return message

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.connection.close()
