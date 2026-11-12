"""Integration tests for the force-resync feature."""

from __future__ import annotations

import asyncio

import pytest

import protocol
from helpers import wait_until


async def test_client_resync_pulls_everyone_to_the_paused_host_position(three_peers):
    room = three_peers
    a, b = room.peers

    await room.host.node.local_action(protocol.MSG_PAUSE, 45.0)
    assert await wait_until(
        lambda: abs(a.player.position - 45.0) < 0.75
        and abs(b.player.position - 45.0) < 0.75,
        2.0,
    )

    # Pretend two peers drifted away from the room.
    a.player.position = 10.0
    b.player.position = 10.0

    await a.node.local_resync()

    assert await wait_until(
        lambda: abs(a.player.position - 45.0) < 0.75
        and abs(b.player.position - 45.0) < 0.75
        and abs(room.host.player.position - 45.0) < 0.75,
        3.0,
    ), "resync must pull every peer onto the host's frame"
    assert not a.player.playing and not b.player.playing
    assert any("Resynced" in item["text"] for item in a.events.of("status"))
    assert any("Resynced" in item["text"] for item in room.host.events.of("status"))
    assert not a.events.has("countdown"), "resync never runs the 3-2-1 countdown"


async def test_host_resync_while_playing_continues_playing(three_peers):
    room = three_peers
    a, b = room.peers

    room.host.player.position = 20.0
    room.host.player.playing = True
    room.host.player.paused = False
    await room.host.node.local_action(protocol.MSG_PLAY, 20.0)
    assert await wait_until(lambda: a.player.playing and b.player.playing, 2.0)

    a.player.position = 5.0
    b.player.position = 5.0

    await room.host.node.local_resync()

    assert await wait_until(
        lambda: abs(a.player.position - 20.0) < 1.0
        and abs(b.player.position - 20.0) < 1.0,
        3.0,
    )
    assert a.player.playing and b.player.playing and room.host.player.playing


async def test_two_simultaneous_resyncs_do_not_oscillate(three_peers):
    room = three_peers
    a, b = room.peers

    room.host.player.position = 60.0
    room.host.player.playing = True
    room.host.player.paused = False
    await room.host.node.local_action(protocol.MSG_PLAY, 60.0)
    assert await wait_until(lambda: a.player.playing and b.player.playing, 2.0)

    await asyncio.gather(a.node.local_resync(), b.node.local_resync())

    assert await wait_until(
        lambda: abs(a.player.position - 60.0) < 1.0
        and abs(b.player.position - 60.0) < 1.0
        and abs(room.host.player.position - 60.0) < 1.0,
        3.0,
    )

    await asyncio.sleep(0.4)
    assert a.player.position == pytest.approx(60.0, abs=0.01)
    assert b.player.position == pytest.approx(60.0, abs=0.01)
