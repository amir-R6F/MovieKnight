"""Pytest fixtures for LANWatch.

The application modules are plain top-level modules (``main.py`` runs them from
inside ``lanwatch/``), so this file puts that directory on ``sys.path``.

Integration fixtures always bind the host to port 0 (the OS picks a free port)
and use FakePlayer, so the tests need neither a fixed port nor mpv.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LANWATCH_DIR = PROJECT_ROOT / "lanwatch"

if str(LANWATCH_DIR) not in sys.path:
    sys.path.insert(0, str(LANWATCH_DIR))

import config  # noqa: E402  (imported after sys.path setup)
from client import ClientNode  # noqa: E402
from helpers import EventCollector, FakePlayer, start_peer  # noqa: E402
from host import HostNode  # noqa: E402


@pytest.fixture(autouse=True)
def fast_handshake(monkeypatch):
    """Two clock-sync rounds instead of five keeps the suite quick."""
    monkeypatch.setattr(config, "CLOCK_SYNC_ROUNDS", 2)


@pytest.fixture
def settings_file(tmp_path, monkeypatch) -> Path:
    """Point LANWatch's settings at a throwaway file for this test."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("LANWATCH_SETTINGS", str(path))
    return path


@dataclass
class Host:
    node: HostNode
    player: FakePlayer
    events: EventCollector


@dataclass
class Peer:
    node: ClientNode
    player: FakePlayer
    events: EventCollector


@dataclass
class Room:
    host: Host
    peers: list[Peer] = field(default_factory=list)

    @property
    def port(self) -> int:
        return self.host.node.port


@pytest.fixture
async def host_node():
    """A started host on a fresh port, with mDNS and mpv stubbed out."""
    events = EventCollector()
    player = FakePlayer(600.0)
    node = HostNode(
        player=player,
        display_name="HostPC",
        port=0,
        on_event=events,
        advertise=False,
    )
    await node.start()
    try:
        yield Host(node=node, player=player, events=events)
    finally:
        with contextlib.suppress(Exception):
            await node.stop()


@pytest.fixture
async def client_node(host_node):
    """One connected client on the host fixture."""
    node, player, events = await start_peer(host_node.node.port, "Peer1")
    try:
        yield Peer(node=node, player=player, events=events)
    finally:
        with contextlib.suppress(Exception):
            await node.disconnect()


@pytest.fixture
async def three_peers(host_node):
    """A host plus two connected clients (three viewers in total)."""
    room = Room(host=host_node)
    for index in range(2):
        node, player, events = await start_peer(
            host_node.node.port, f"Peer{index + 1}"
        )
        room.peers.append(Peer(node=node, player=player, events=events))

    try:
        yield room
    finally:
        for peer in room.peers:
            with contextlib.suppress(Exception):
                await peer.node.disconnect()
