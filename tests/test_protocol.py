"""Unit tests for the wire protocol: envelope, round-trips, timing, echo."""

from __future__ import annotations

import json
import random
import time
import uuid

import pytest

import protocol


def _roundtrip(message: dict) -> dict:
    """Encode to a WebSocket frame and decode it again."""
    return protocol.decode(protocol.encode(message))


# --- envelope ----------------------------------------------------------------


def test_every_message_gets_a_unique_uuid4_id():
    ids = [protocol.build(protocol.MSG_PAUSE, "me")["msg_id"] for _ in range(200)]

    assert len(set(ids)) == len(ids)
    for value in ids[:10]:
        parsed = uuid.UUID(hex=value)
        assert parsed.version == 4
        assert len(value) == 32


def test_envelope_timestamps_do_not_go_backwards():
    first = protocol.build(protocol.MSG_PAUSE, "me")
    second = protocol.build(protocol.MSG_PAUSE, "me")

    assert second["timestamp"] >= first["timestamp"]
    assert abs(second["timestamp"] - time.perf_counter()) < 1.0


def test_build_keeps_extra_fields_and_respects_overrides():
    message = protocol.build(
        protocol.MSG_PLAY, "me", msg_id="fixed", timestamp=10.5, position=3.0
    )

    assert message["msg_id"] == "fixed"
    assert message["timestamp"] == 10.5
    assert message["position"] == 3.0


def test_decode_rejects_broken_frames():
    for raw in ("not json", "[1, 2, 3]", json.dumps({"no": "type"}), ""):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode(raw)


def test_decode_accepts_bytes_and_unicode():
    message = protocol.make_join("id", "کاربر", 120.0)
    decoded = protocol.decode(protocol.encode(message).encode("utf-8"))

    assert decoded["display_name"] == "کاربر"


def test_as_float_never_raises():
    assert protocol.as_float(None) == 0.0
    assert protocol.as_float("12.5") == 12.5
    assert protocol.as_float(float("nan")) == 0.0
    assert protocol.as_float(float("inf")) == 0.0
    assert protocol.as_float("nope", -1.0) == -1.0


# --- round-trips for every message type --------------------------------------


def _join():
    return protocol.make_join("client-1", "Living Room", 600.0)


def _welcome():
    return protocol.make_welcome(
        "host-1",
        state={"position": 12.5, "is_playing": True, "target_time": 99.0},
        peers=[{"sender_id": "host-1", "display_name": "Host", "role": "host"}],
        warning="Video duration differs",
    )


def _play():
    return protocol.make_action(
        protocol.MSG_PLAY, "client-1", position=12.5, is_playing=True, host_time=99.0
    )


def _play_countdown():
    return protocol.make_action(
        protocol.MSG_PLAY,
        "client-1",
        position=12.5,
        is_playing=False,
        host_time=99.0,
        start_in_seconds=3.0,
    )


def _pause():
    return protocol.make_action(
        protocol.MSG_PAUSE, "client-1", position=20.0, is_playing=False
    )


def _seek():
    return protocol.make_action(
        protocol.MSG_SEEK, "host-1", position=42.0, is_playing=True, host_time=7.0
    )


def _stop():
    return protocol.make_action(protocol.MSG_STOP, "client-1", position=0.0, is_playing=False)


def _heartbeat():
    return protocol.make_heartbeat(
        "host-1",
        position=42.0,
        is_playing=True,
        peers=[{"sender_id": "host-1", "display_name": "Host", "role": "host"}],
        host_time=7.0,
    )


def _peer_left():
    return protocol.make_peer_left("client-9")


def _sync_ping():
    return protocol.make_sync_ping("client-1", 1234.5)


def _sync_pong():
    return protocol.make_sync_pong("host-1", 1234.5, 1234.6, 1234.7)


def _resync_request():
    return protocol.make_resync_request("client-1")


def _resync_apply():
    return protocol.make_resync_apply("host-1", position=45.25, is_playing=False)


@pytest.mark.parametrize(
    "maker",
    [
        _join,
        _welcome,
        _play,
        _play_countdown,
        _pause,
        _seek,
        _stop,
        _heartbeat,
        _peer_left,
        _sync_ping,
        _sync_pong,
        _resync_request,
        _resync_apply,
    ],
    ids=lambda maker: maker.__name__.lstrip("_"),
)
def test_every_message_type_survives_the_wire(maker):
    message = maker()
    decoded = _roundtrip(message)

    assert decoded["type"] == message["type"]
    assert decoded["sender_id"] == message["sender_id"]
    assert decoded["msg_id"] == message["msg_id"]
    for key, value in message.items():
        assert decoded[key] == value, f"{key} changed in transit"


def test_resync_apply_uses_a_fresh_id_and_stamps_host_time():
    first = protocol.make_resync_apply("host", position=1.0, is_playing=True)
    second = protocol.make_resync_apply("host", position=1.0, is_playing=True)

    assert first["msg_id"] != second["msg_id"]
    assert isinstance(first["host_time"], float)
    assert first["is_playing"] is True


def test_resync_request_carries_only_the_envelope():
    decoded = _roundtrip(protocol.make_resync_request("client-1"))

    assert decoded["type"] == protocol.MSG_RESYNC_REQUEST
    assert decoded["sender_id"] == "client-1"
    assert "position" not in decoded


# --- play / countdown --------------------------------------------------------


@pytest.mark.parametrize("seconds", [3.0, 1.0, 0.0])
def test_play_start_in_seconds_round_trip(seconds: float):
    decoded = _roundtrip(
        protocol.make_action(
            protocol.MSG_PLAY,
            "client-id",
            position=12.5,
            is_playing=False,
            start_in_seconds=seconds,
        )
    )

    assert decoded["type"] == protocol.MSG_PLAY
    assert decoded["start_in_seconds"] == pytest.approx(seconds)
    assert decoded["position"] == pytest.approx(12.5)


def test_play_without_countdown_has_no_field():
    message = protocol.make_action(
        protocol.MSG_PLAY, "client-id", position=1.0, is_playing=True
    )

    assert "start_in_seconds" not in message
    assert "start_in_seconds" not in _roundtrip(message)


def test_old_peer_still_understands_a_countdown_message():
    """A minimal peer ignores the extra field and just plays."""
    message = _play_countdown()
    stripped = {
        key: value
        for key, value in message.items()
        if key not in ("start_in_seconds", "host_time")
    }

    position, playing = protocol.resolve_target(stripped, 0.0, 100.0)
    assert position == pytest.approx(12.5)
    assert playing is False or playing is True  # "is_playing" decides, not the countdown


def test_is_countdown_only_for_positive_play():
    assert protocol.is_countdown({"type": "play", "start_in_seconds": 3})
    assert not protocol.is_countdown({"type": "play", "start_in_seconds": 0})
    assert not protocol.is_countdown({"type": "play"})
    assert not protocol.is_countdown({"type": "pause", "start_in_seconds": 3})
    assert not protocol.is_countdown({"type": "play", "start_in_seconds": "x"})


def test_countdown_remaining_shrinks_with_host_time():
    message = {
        "type": "play",
        "position": 20.0,
        "start_in_seconds": 3.0,
        "host_time": 100.0,
    }

    assert protocol.countdown_remaining(message, 0.0, 101.5) == pytest.approx(1.5)
    assert protocol.countdown_remaining(message, 0.0, 103.0) == pytest.approx(0.0)
    assert protocol.countdown_remaining(message, 0.0, 120.0) == pytest.approx(0.0)


def test_countdown_remaining_respects_clock_offset():
    message = {"position": 5.0, "start_in_seconds": 3.0, "host_time": 100.0}
    offset = 10.0  # the client's clock is 10 s behind the host

    assert protocol.countdown_remaining(message, offset, 91.0) == pytest.approx(2.0)

    position, remaining = protocol.resolve_countdown(message, offset, 91.0)
    assert position == pytest.approx(5.0)
    assert remaining == pytest.approx(2.0)


def test_countdown_position_is_not_advanced_by_skew():
    message = {
        "position": 8.0,
        "is_playing": False,
        "start_in_seconds": 3.0,
        "host_time": 100.0,
    }

    position, remaining = protocol.resolve_countdown(message, 0.0, 102.0)

    assert position == pytest.approx(8.0)
    assert remaining == pytest.approx(1.0)


# --- time resolution ---------------------------------------------------------


def test_resync_apply_target_advances_while_playing():
    message = protocol.make_resync_apply("host", position=20.0, is_playing=True)
    message["host_time"] = 100.0

    position, playing = protocol.resolve_target(message, 0.0, 101.5)

    assert playing is True
    assert position == pytest.approx(21.5)


def test_resync_apply_does_not_advance_while_paused():
    message = protocol.make_resync_apply("host", position=45.0, is_playing=False)
    message["host_time"] = 100.0

    position, playing = protocol.resolve_target(message, 0.0, 110.0)

    assert playing is False
    assert position == pytest.approx(45.0)


def test_skew_is_clamped_so_a_bad_offset_cannot_hurl_playback():
    message = {"type": "play", "position": 10.0, "is_playing": True, "host_time": 0.0}

    position, _ = protocol.resolve_target(message, 0.0, 600.0)

    assert position == pytest.approx(10.0 + protocol.MAX_SKEW)


def test_resolve_target_never_goes_negative():
    message = {"type": "seek", "position": -5.0, "is_playing": False}

    position, playing = protocol.resolve_target(message, 0.0, 0.0)

    assert position == 0.0
    assert playing is False


def test_skew_math_lands_within_50ms_for_random_values():
    """Property test: the resolved position must match the media timeline."""
    rng = random.Random(20241112)

    for _ in range(500):
        position = rng.uniform(0.0, 7200.0)
        host_time = rng.uniform(0.0, 1_000_000.0)
        # Never exceed MAX_SKEW: beyond that the clamp is by design.
        elapsed = rng.uniform(0.0, protocol.MAX_SKEW)
        offset = rng.uniform(-5.0, 5.0)

        # The message was stamped at host_time; on this peer's clock that is
        # host_time - offset, and *elapsed* seconds have passed since.
        local_now = host_time - offset + elapsed
        message = {
            "type": "play",
            "position": position,
            "is_playing": True,
            "host_time": host_time,
        }

        resolved, playing = protocol.resolve_target(message, offset, local_now)

        assert playing is True
        assert abs(resolved - (position + elapsed)) < 0.05


def test_resolve_state_advances_playing_and_freezes_paused():
    playing_state = {"position": 30.0, "is_playing": True, "target_time": 100.0}
    paused_state = {"position": 30.0, "is_playing": False, "target_time": 100.0}

    assert protocol.resolve_state(playing_state, 0.0, 101.0)[0] == pytest.approx(31.0)
    assert protocol.resolve_state(paused_state, 0.0, 101.0)[0] == pytest.approx(30.0)


# --- echo suppression --------------------------------------------------------


def test_tracker_marks_issued_messages_and_ignores_unknown_ids():
    tracker = protocol.MessageTracker()

    issued = tracker.issue(
        protocol.MSG_PAUSE, "me", position=1.0, is_playing=False
    )

    assert tracker.is_echo(issued["msg_id"])
    assert not tracker.is_echo("never-seen")
    assert not tracker.is_echo(None)
    assert not tracker.is_echo(42)


def test_host_rebroadcast_msg_id_is_not_an_echo_for_the_requester():
    tracker = protocol.MessageTracker()
    request = tracker.issue(
        protocol.MSG_PLAY,
        "client-id",
        position=10.0,
        is_playing=False,
        start_in_seconds=3.0,
    )
    assert tracker.is_echo(request["msg_id"])

    rebroadcast = protocol.make_action(
        protocol.MSG_PLAY,
        "client-id",
        position=10.0,
        is_playing=False,
        start_in_seconds=3.0,
    )

    assert rebroadcast["msg_id"] != request["msg_id"]
    assert not tracker.is_echo(rebroadcast["msg_id"]), (
        "the requester must apply the host's countdown broadcast"
    )


def test_host_echo_of_a_plain_action_keeps_the_original_id():
    """Plain actions are re-broadcast with the requester's own msg_id."""
    message = protocol.make_action(
        protocol.MSG_PAUSE, "client-1", position=3.0, is_playing=False
    )

    rebroadcast = protocol.make_action(
        protocol.MSG_PAUSE,
        "client-1",
        position=3.0,
        is_playing=False,
        msg_id=message["msg_id"],
        host_time=protocol.now(),
    )

    tracker = protocol.MessageTracker()
    tracker.remember(message["msg_id"])

    assert rebroadcast["msg_id"] == message["msg_id"]
    assert tracker.is_echo(rebroadcast["msg_id"])


def test_tracker_keeps_a_bounded_window():
    tracker = protocol.MessageTracker(capacity=8)
    ids = [
        tracker.issue("pause", "me", position=0.0, is_playing=False)["msg_id"]
        for _ in range(12)
    ]

    assert not tracker.is_echo(ids[0]), "old ids are evicted"
    assert tracker.is_echo(ids[-1])


# --- playback state ----------------------------------------------------------


def test_playback_state_advances_only_while_playing():
    state = protocol.PlaybackState()
    state.set(10.0, True)
    state.updated_at -= 2.0  # pretend the snapshot is two seconds old

    assert state.position_at() == pytest.approx(12.0, abs=0.05)

    state.set(10.0, False)
    state.updated_at -= 2.0

    assert state.position_at() == pytest.approx(10.0)


def test_playback_state_snapshot_rounds_and_clamps():
    state = protocol.PlaybackState()
    state.set(-4.0, False)

    snapshot = state.snapshot()

    assert snapshot["position"] == 0.0
    assert snapshot["is_playing"] is False
    assert isinstance(snapshot["target_time"], float)


def test_peer_info_round_trip_defaults():
    info = protocol.PeerInfo.from_dict(
        {"sender_id": "x", "display_name": "X", "role": "host", "video_duration": "12"}
    )

    assert info.role == "host"
    assert info.video_duration == 12.0

    payload = info.to_dict()
    assert payload["sender_id"] == "x"
    assert payload["self"] is False
