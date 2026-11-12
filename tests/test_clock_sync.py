"""Unit tests for the NTP-style clock offset estimation."""

from __future__ import annotations

import asyncio
import random

import pytest

import protocol
from clock_sync import ClockSync, ClockSyncError, answer_ping

#: The host clock is this far ahead of ours; every simulation below hides it.
GROUND_TRUTH = 12.345


def _pong(t1: float, delay: float, *, offset: float = GROUND_TRUTH) -> dict:
    """A pong for a symmetric round trip of *delay* seconds."""
    half = delay / 2.0
    received = t1 + offset + half  # host clock at "receive" time
    return {"t1": t1, "t2": received, "t3": received}


async def test_five_rounds_estimate_the_offset_within_10ms():
    rng = random.Random(7)

    async def ping(t1: float) -> dict:
        delay = rng.uniform(0.0, 0.050)  # 0-50 ms round trip
        await asyncio.sleep(delay)
        return _pong(t1, delay)

    estimator = ClockSync(rounds=5, timeout=1.0)
    sample = await estimator.measure(ping)

    assert len(estimator.samples) == 5
    assert sample.offset == pytest.approx(GROUND_TRUTH, abs=0.010)
    assert estimator.offset == pytest.approx(GROUND_TRUTH, abs=0.010)
    assert all(entry.rtt >= 0.0 for entry in estimator.samples)


async def test_median_filter_drops_one_500ms_spike():
    calls = {"count": 0}

    async def ping(t1: float) -> dict:
        calls["count"] += 1
        if calls["count"] == 3:
            # The host answered instantly, but the reply sat in a buffer for
            # half a second: this sample's offset is 250 ms too low.
            await asyncio.sleep(0.5)
            return _pong(t1, 0.0)
        await asyncio.sleep(0.002)
        return _pong(t1, 0.002)

    estimator = ClockSync(rounds=5, timeout=2.0)
    sample = await estimator.measure(ping)

    assert calls["count"] == 5
    assert sample.offset == pytest.approx(GROUND_TRUTH, abs=0.010), (
        "one bad sample must not move the median"
    )


async def test_all_failed_rounds_raise():
    async def ping(t1: float) -> dict:
        raise ValueError("no route to host")

    with pytest.raises(ClockSyncError):
        await ClockSync(rounds=3, timeout=0.2).measure(ping)


async def test_malformed_pongs_are_skipped():
    async def ping(t1: float) -> dict:
        if t1:
            return {"t1": t1}  # no t2/t3
        return {}

    with pytest.raises(ClockSyncError):
        await ClockSync(rounds=2, timeout=0.2).measure(ping)


def test_answer_ping_echoes_t1_exactly():
    ping = protocol.make_sync_ping("client-1", 987.654)

    pong = answer_ping("host-1", ping)

    assert pong["type"] == protocol.MSG_SYNC_PONG
    assert pong["sender_id"] == "host-1"
    assert pong["t1"] == pytest.approx(987.654)
    assert pong["t2"] <= pong["t3"]


def test_answer_ping_survives_a_ping_without_t1():
    pong = answer_ping("host-1", {"type": protocol.MSG_SYNC_PING})

    assert isinstance(pong["t1"], float)
    assert pong["t1"] == pytest.approx(pong["t2"])


def test_clock_helpers_are_inverses():
    clock = ClockSync()

    clock.offset = 3.5

    assert clock.host_to_local(10.0) == pytest.approx(6.5)
    assert clock.local_to_host(6.5) == pytest.approx(10.0)
    assert clock.local_to_host(clock.host_to_local(42.0)) == pytest.approx(42.0)


def test_reset_clears_every_sample():
    clock = ClockSync()
    clock.offset = 2.0
    clock.rtt = 0.01
    clock.samples.append(object())  # type: ignore[arg-type]

    clock.reset()

    assert clock.offset == 0.0
    assert clock.rtt == 0.0
    assert clock.samples == []
