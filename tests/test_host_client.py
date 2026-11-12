"""Integration tests: a real WebSocket host and real clients, fake players.

Every test binds the host to port 0, so the suite never fights over a port.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import config
import protocol
from helpers import RawPeer, start_peer, wait_until


# --- basic propagation -------------------------------------------------------


async def test_pause_on_client_a_pauses_everyone(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PAUSE, 12.0)

    assert await wait_until(
        lambda: abs(a.player.position - 12.0) < 0.75
        and abs(b.player.position - 12.0) < 0.75
        and abs(room.host.player.position - 12.0) < 0.75
        and not a.player.playing
        and not b.player.playing
        and not room.host.player.playing,
        2.0,
    ), "every peer must land on the same paused frame"


async def test_play_on_client_b_resumes_everyone(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PAUSE, 12.0)
    # Wait for the seek, not just the pause: every fake player starts paused
    # at 0 s, so "not playing" alone would already be true.
    assert await wait_until(
        lambda: abs(b.player.position - 12.0) < 0.75 and not b.player.playing, 2.0
    )

    await b.node.local_action(protocol.MSG_PLAY)

    assert await wait_until(
        lambda: a.player.playing and b.player.playing and room.host.player.playing,
        2.0,
    )
    assert abs(a.player.position - 12.0) < 0.75


async def test_seek_on_host_reaches_both_clients(three_peers):
    room = three_peers
    a, b = room.peers

    await room.host.node.local_action(protocol.MSG_SEEK, 55.0)

    assert await wait_until(
        lambda: abs(a.player.position - 55.0) < 0.3
        and abs(b.player.position - 55.0) < 0.3,
        2.0,
    )
    assert not a.player.playing and not b.player.playing


async def test_stop_from_a_client_resets_everyone_to_zero(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PLAY, 40.0)
    assert await wait_until(lambda: b.player.playing, 2.0)

    await a.node.local_action(protocol.MSG_STOP)

    assert await wait_until(
        lambda: a.player.position == 0.0
        and b.player.position == 0.0
        and room.host.player.position == 0.0,
        2.0,
    )
    assert not a.player.playing and not b.player.playing


async def test_client_does_not_reapply_its_own_pause_echo(three_peers):
    room = three_peers
    a, _ = room.peers

    before = len(a.player.applies)

    await a.node.local_action(protocol.MSG_PAUSE, 20.0)
    await asyncio.sleep(0.3)

    assert len(a.player.applies) == before + 1, (
        "the host's echo of our own pause must be suppressed"
    )
    assert a.player.position == pytest.approx(20.0)
    assert not a.player.playing


# --- joins -------------------------------------------------------------------


async def test_late_join_mid_playback_seeks_to_the_host(three_peers):
    room = three_peers

    await room.host.node.local_action(protocol.MSG_PLAY, 30.0)
    assert await wait_until(lambda: room.peers[0].player.playing, 2.0)

    late_node, late_player, late_events = await start_peer(room.port, "Late")

    try:
        assert late_player.playing is True
        assert abs(late_player.position - 30.0) < 2.0
        assert late_events.has("state")
    finally:
        await late_node.disconnect()


async def test_late_join_while_paused_inherits_the_pause(three_peers):
    room = three_peers

    await room.host.node.local_action(protocol.MSG_PAUSE, 45.0)
    assert await wait_until(lambda: not room.peers[1].player.playing, 2.0)

    late_node, late_player, _ = await start_peer(room.port, "Late")

    try:
        assert late_player.playing is False
        assert late_player.position == pytest.approx(45.0, abs=0.5)
    finally:
        await late_node.disconnect()


async def test_duration_mismatch_warns_host_and_joiner(three_peers):
    room = three_peers

    node, _, events = await start_peer(room.port, "OddVideo", duration=400.0)

    try:
        host_warnings = [item["text"] for item in room.host.events.of("warning")]
        assert any("differs" in text for text in host_warnings), host_warnings
        assert any("differs" in item["text"] for item in events.of("warning"))
    finally:
        await node.disconnect()


# --- departures --------------------------------------------------------------


async def test_graceful_disconnect_broadcasts_peer_left(three_peers):
    room = three_peers
    a, b = room.peers

    await room.host.node.local_action(protocol.MSG_PLAY, 5.0)
    assert await wait_until(lambda: b.player.playing, 2.0)

    observer = await RawPeer.join(room.port, "Observer")
    try:
        await a.node.disconnect()

        message = await observer.next(protocol.MSG_PEER_LEFT)
        assert message["peer_id"] == a.node.sender_id

        assert await wait_until(
            lambda: all(
                peer["sender_id"] != a.node.sender_id
                for peer in room.host.node._peer_payload()
            ),
            2.0,
        )
        assert b.player.playing, "a departure must not stop playback for others"
    finally:
        await observer.close()


async def test_host_death_disconnects_and_pauses_every_client(three_peers):
    room = three_peers
    a, b = room.peers

    await room.host.node.local_action(protocol.MSG_PLAY, 25.0)
    assert await wait_until(lambda: a.player.playing and b.player.playing, 2.0)

    # Abrupt death: drop the server and its loops without a graceful shutdown.
    server = room.host.node._server
    room.host.node._server = None
    server.close(close_connections=True)
    for task in list(room.host.node._tasks):
        task.cancel()
    await server.wait_closed()

    assert await wait_until(
        lambda: a.events.last("connection") is not None
        and a.events.last("connection")["connected"] is False
        and b.events.last("connection")["connected"] is False
        and not a.player.playing
        and not b.player.playing,
        3.0,
    )


# --- concurrency and stress --------------------------------------------------


async def test_five_clients_receive_the_pause_within_200ms(host_node):
    port = host_node.node.port
    peers = await asyncio.gather(*(start_peer(port, f"Peer{i}") for i in range(5)))

    try:
        started = time.perf_counter()
        await host_node.node.local_action(protocol.MSG_PAUSE, 7.0)
        arrived = await wait_until(
            lambda: all(
                not player.playing and abs(player.position - 7.0) < 0.75
                for _, player, _ in peers
            ),
            timeout=1.0,
        )
        elapsed = time.perf_counter() - started

        assert arrived, "a pause must reach all five clients"
        assert elapsed < 0.2, f"broadcast took {elapsed * 1000:.0f} ms"
    finally:
        await asyncio.gather(*(node.disconnect() for node, _, _ in peers))


async def test_twenty_rapid_seeks_settle_on_the_last_position(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PAUSE, 0.0)

    for index in range(1, 21):
        await a.node.local_action(protocol.MSG_SEEK, float(index))
        await asyncio.sleep(0.01)

    assert await wait_until(
        lambda: abs(a.player.position - 20.0) < 0.2
        and abs(b.player.position - 20.0) < 0.2
        and abs(room.host.player.position - 20.0) < 0.2,
        3.0,
    )

    await asyncio.sleep(0.3)
    assert a.player.position == pytest.approx(20.0)
    assert b.player.position == pytest.approx(20.0), "no oscillation after the storm"


async def test_pause_then_immediate_play_ends_playing(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PAUSE, 30.0)
    await a.node.local_action(protocol.MSG_PLAY)  # no await gap beyond the round trip

    assert await wait_until(
        lambda: a.player.playing and b.player.playing and room.host.player.playing,
        2.0,
    )
    assert abs(b.player.position - 30.0) < 1.0


async def test_seek_while_paused_stays_paused(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PAUSE, 10.0)
    assert await wait_until(lambda: not b.player.playing, 2.0)

    await a.node.local_action(protocol.MSG_SEEK, 40.0)

    assert await wait_until(
        lambda: abs(b.player.position - 40.0) < 0.5, 2.0
    )
    assert not b.player.playing and not room.host.player.playing


# --- hostile input -----------------------------------------------------------


async def test_malformed_json_is_ignored_and_the_host_survives(three_peers):
    room = three_peers
    a = room.peers[0]

    raw = await RawPeer.join(room.port, "Garbage")
    try:
        await raw.send_raw("this is not json at all")
        await raw.send_raw("[1, 2, 3]")
        await raw.send_raw(json.dumps({"missing": "type"}))

        assert room.host.node.is_hosting is True

        await a.node.local_action(protocol.MSG_PAUSE, 5.0)
        assert await wait_until(
            lambda: not a.player.playing and abs(a.player.position - 5.0) < 0.5, 2.0
        )
    finally:
        await raw.close()


async def test_silent_client_is_dropped_from_the_peer_list(host_node, monkeypatch):
    monkeypatch.setattr(config, "PEER_TIMEOUT", 0.4)
    monkeypatch.setattr(config, "HEARTBEAT_INTERVAL", 0.1)

    chatty, _, chatty_events = await start_peer(host_node.node.port, "Chatty")
    silent = await RawPeer.join(host_node.node.port, "Silent")

    try:
        assert any(
            peer["sender_id"] == silent.sender_id
            for peer in host_node.node._peer_payload()
        )

        assert await wait_until(
            lambda: all(
                peer["sender_id"] != silent.sender_id
                for peer in host_node.node._peer_payload()
            ),
            3.0,
        ), "a client that never sends a heartbeat must be evicted"

        assert await wait_until(
            lambda: chatty_events.last("peers") is not None
            and all(
                peer["sender_id"] != silent.sender_id
                for peer in chatty_events.last("peers")["peers"]
            ),
            2.0,
        ), "the remaining peers must be told"
    finally:
        await silent.close()
        await chatty.disconnect()
