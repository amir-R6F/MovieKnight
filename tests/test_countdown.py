"""Integration tests for the 3-2-1 countdown before play."""

from __future__ import annotations

import asyncio

import pytest

import protocol
from helpers import RawPeer, wait_until


async def test_countdown_play_keeps_the_field_and_gets_a_fresh_msg_id(three_peers):
    room = three_peers
    a, b = room.peers

    observer = await RawPeer.join(room.port, "Observer")
    try:
        await a.node.local_action(protocol.MSG_PLAY, 5.0, start_in_seconds=3.0)

        broadcast = await observer.next(protocol.MSG_PLAY)

        assert broadcast["start_in_seconds"] == pytest.approx(3.0)
        assert broadcast["sender_id"] == a.node.sender_id
        assert not a.node._tracker.is_echo(broadcast["msg_id"]), (
            "the requester must run the countdown too"
        )

        assert await wait_until(lambda: b.events.has("countdown"), 2.0)
        remaining = b.events.of("countdown")[-1]["remaining"]
        assert 2.3 <= remaining <= 3.0
        assert abs(b.player.position - 5.0) < 0.5
        assert not b.player.playing
        assert await wait_until(lambda: room.host.node._countdown_task is not None, 1.0)
    finally:
        await observer.close()


async def test_play_with_zero_countdown_starts_immediately(three_peers):
    room = three_peers
    a, b = room.peers

    observer = await RawPeer.join(room.port, "Observer")
    try:
        await a.node.local_action(protocol.MSG_PLAY, 7.0, start_in_seconds=0.0)

        broadcast = await observer.next(protocol.MSG_PLAY)
        assert "start_in_seconds" not in broadcast, (
            "an immediate play must stay backward compatible"
        )

        assert await wait_until(
            lambda: a.player.playing and b.player.playing, 2.0
        )
        assert abs(a.player.position - 7.0) < 0.75
        assert not b.events.has("countdown")
    finally:
        await observer.close()


async def test_countdown_ends_with_everyone_playing_together(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PLAY, 10.0, start_in_seconds=0.5)

    assert await wait_until(
        lambda: a.events.has("countdown") and b.events.has("countdown"), 2.0
    )
    assert await wait_until(
        lambda: a.player.playing and b.player.playing and room.host.player.playing,
        3.0,
    ), "everyone must unpause when the countdown reaches GO"
    assert await wait_until(
        lambda: a.events.has("countdown_finished") and room.host.events.has("countdown_finished"),
        1.0,
    )
    assert abs(a.player.position - 10.0) < 1.0
    assert abs(b.player.position - 10.0) < 1.0


async def test_pause_during_the_countdown_cancels_it_everywhere(three_peers):
    room = three_peers
    a, b = room.peers

    await a.node.local_action(protocol.MSG_PLAY, 10.0, start_in_seconds=0.6)
    assert await wait_until(lambda: b.events.has("countdown"), 2.0)

    await b.node.local_action(protocol.MSG_PAUSE, b.player.position)

    assert await wait_until(
        lambda: b.events.has("countdown_cancel")
        and room.host.events.has("countdown_cancel"),
        2.0,
    )

    await asyncio.sleep(0.9)
    assert not a.player.playing and not b.player.playing
    assert not b.events.has("countdown_finished")
    assert not room.host.events.has("countdown_finished")


async def test_resync_during_the_countdown_applies_instantly(three_peers):
    room = three_peers
    a, _ = room.peers

    await a.node.local_action(protocol.MSG_PLAY, 4.0, start_in_seconds=0.8)
    assert await wait_until(lambda: room.host.node._countdown_task is not None, 1.0)

    await room.host.node.local_resync()

    assert await wait_until(
        lambda: room.host.events.has("countdown_cancel"), 1.0
    )

    await asyncio.sleep(0.6)
    assert not room.host.player.playing, "a resync must not let GO fire afterwards"


async def test_play_while_already_playing_is_a_noop(three_peers):
    room = three_peers
    a, b = room.peers

    await room.host.node.local_action(protocol.MSG_PLAY, 3.0)
    assert await wait_until(lambda: a.player.playing and b.player.playing, 2.0)

    applies_before = len(a.player.applies)

    await a.node.local_action(protocol.MSG_PLAY, 99.0, start_in_seconds=3.0)
    await asyncio.sleep(0.3)

    assert len(a.player.applies) == applies_before, "no seek, no pause, no message"
    assert not a.events.has("countdown")
    assert not b.events.has("countdown")
    assert abs(a.player.position - 3.0) < 0.75
