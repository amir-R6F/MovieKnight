"""LANWatch wire protocol: message schemas, serialization and echo tracking.

Everything sent between peers is a small JSON object. Every message carries the
common envelope fields:

    type       message kind (see the MSG_* constants)
    msg_id     a uuid4 hex string, unique per message
    sender_id  a uuid4 string identifying the peer that created the message
    timestamp  the sender's monotonic clock (time.perf_counter) when it was built

The host is the only broadcaster: clients send actions to the host, which
re-broadcasts them to every client *including* the originator. The originator
ignores its own echo (see MessageTracker), because it already applied the
action locally on the key press.

A few fields go beyond the minimal schema so that peers can line up playback
precisely (all of them additive, so a minimal peer still works):

    host_time   the host's monotonic clock at broadcast time. Used together with
                the measured clock offset to work out how long ago the command
                was issued, and therefore where playback should be *now*.
    is_playing  playback state after the action, so a seek can be resolved
                correctly whether it happened while playing or paused.
    peers       peer list snapshot, carried by welcome/heartbeat.
    start_in_seconds
                on a play message: seconds of countdown before playback
                starts. Missing or 0 means "play right away" (and is what an
                older peer understands), so the field is fully backward
                compatible.

Resync is the host's authoritative "everybody jump here" message. Peers ask
for one with ``resync_request``; the host answers with ``resync_apply``, which
always carries the host's own fresh msg_id so nobody suppresses it as an echo.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable

# --- message types -----------------------------------------------------------

MSG_JOIN = "join"
MSG_WELCOME = "welcome"
MSG_PLAY = "play"
MSG_PAUSE = "pause"
MSG_SEEK = "seek"
MSG_STOP = "stop"
MSG_HEARTBEAT = "heartbeat"
MSG_PEER_LEFT = "peer_left"
MSG_SYNC_PING = "sync_ping"
MSG_SYNC_PONG = "sync_pong"
MSG_RESYNC_REQUEST = "resync_request"
MSG_RESYNC_APPLY = "resync_apply"

#: Message types that describe a playback action and get re-broadcast.
ACTIONS = frozenset({MSG_PLAY, MSG_PAUSE, MSG_SEEK, MSG_STOP})

#: Actions after which the target position must be hit exactly (no "close
#: enough" optimisation), because the user aimed at a specific spot.
EXACT_ACTIONS = frozenset({MSG_SEEK, MSG_STOP})


class ProtocolError(ValueError):
    """Raised when an incoming frame is not a usable LANWatch message."""


# --- clock helpers -----------------------------------------------------------


def now() -> float:
    """Monotonic seconds, shared by every timestamp in the protocol."""
    return time.perf_counter()


def new_id() -> str:
    """A fresh uuid4 hex id for a message or a peer."""
    return uuid.uuid4().hex


def as_float(value: Any, default: float = 0.0) -> float:
    """Best-effort float conversion; never raises, rejects NaN/inf."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


# --- envelope ----------------------------------------------------------------


def build(
    msg_type: str,
    sender_id: str,
    *,
    msg_id: str | None = None,
    timestamp: float | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a protocol message with the common envelope fields filled in."""
    message: dict[str, Any] = {
        "type": msg_type,
        "msg_id": msg_id or new_id(),
        "sender_id": sender_id,
        "timestamp": now() if timestamp is None else float(timestamp),
    }
    message.update(fields)
    return message


def encode(message: dict[str, Any]) -> str:
    """Serialize a message for WebSocket transport."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parse and minimally validate an incoming frame.

    Raises ProtocolError for anything that is not a JSON object with a string
    ``type``; callers decide whether that is worth logging or closing over.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        message = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")
    if not isinstance(message.get("type"), str):
        raise ProtocolError("message is missing a string 'type'")
    return message


# --- typed helpers -----------------------------------------------------------


@dataclass(slots=True)
class PeerInfo:
    """One entry of a peer list, as shown in the GUI."""

    sender_id: str
    display_name: str
    role: str = "client"
    video_duration: float | None = None
    is_self: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender_id": self.sender_id,
            "display_name": self.display_name,
            "role": self.role,
            "video_duration": self.video_duration,
            "self": self.is_self,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeerInfo":
        duration = data.get("video_duration")
        return cls(
            sender_id=str(data.get("sender_id") or ""),
            display_name=str(data.get("display_name") or "Peer"),
            role="host" if data.get("role") == "host" else "client",
            video_duration=as_float(duration) if duration is not None else None,
            is_self=bool(data.get("self")),
        )


@dataclass(slots=True)
class PlaybackState:
    """The host's authoritative playback state on the host's monotonic clock."""

    is_playing: bool = False
    position: float = 0.0
    updated_at: float = field(default_factory=now)

    def set(self, position: float, is_playing: bool) -> None:
        """Adopt a new state at the current instant."""
        self.position = max(0.0, as_float(position))
        self.is_playing = bool(is_playing)
        self.updated_at = now()

    def position_at(self, when: float | None = None) -> float:
        """Position at *when* (host clock), advancing while playing."""
        reference = now() if when is None else when
        position = self.position
        if self.is_playing:
            position += max(0.0, reference - self.updated_at)
        return max(0.0, position)

    def snapshot(self, when: float | None = None) -> dict[str, Any]:
        """Serializable state plus the host-clock instant it refers to."""
        reference = now() if when is None else when
        return {
            "is_playing": self.is_playing,
            "position": round(self.position_at(reference), 3),
            "target_time": reference,
        }


def make_join(
    sender_id: str, display_name: str, video_duration: float | None
) -> dict[str, Any]:
    """A joiner's first message."""
    return build(
        MSG_JOIN,
        sender_id,
        display_name=display_name,
        video_duration=video_duration,
    )


def make_welcome(
    host_id: str,
    *,
    state: dict[str, Any],
    peers: Iterable[dict[str, Any]],
    warning: str | None = None,
) -> dict[str, Any]:
    """The host's answer to a join: current state plus the peer list."""
    return build(
        MSG_WELCOME,
        host_id,
        state=state,
        peers=list(peers),
        warning=warning,
    )


def make_action(
    action: str,
    sender_id: str,
    *,
    position: float,
    is_playing: bool,
    msg_id: str | None = None,
    host_time: float | None = None,
    start_in_seconds: float | None = None,
) -> dict[str, Any]:
    """A play/pause/seek/stop message.

    ``start_in_seconds`` is only written when the caller supplies it, so plain
    messages stay byte-for-byte what older peers expect.
    """
    fields: dict[str, Any] = {
        "position": round(max(0.0, as_float(position)), 3),
        "is_playing": bool(is_playing),
    }
    if host_time is not None:
        fields["host_time"] = float(host_time)
    if start_in_seconds is not None:
        fields["start_in_seconds"] = max(0.0, as_float(start_in_seconds))
    return build(action, sender_id, msg_id=msg_id, **fields)


def make_heartbeat(
    sender_id: str,
    *,
    position: float | None = None,
    is_playing: bool | None = None,
    peers: Iterable[dict[str, Any]] | None = None,
    host_time: float | None = None,
) -> dict[str, Any]:
    """A liveness message. The host's version also carries state and peers."""
    fields: dict[str, Any] = {}
    if position is not None:
        fields["position"] = round(max(0.0, as_float(position)), 3)
    if is_playing is not None:
        fields["is_playing"] = bool(is_playing)
    if peers is not None:
        fields["peers"] = list(peers)
    if host_time is not None:
        fields["host_time"] = float(host_time)
    return build(MSG_HEARTBEAT, sender_id, **fields)


def make_peer_left(left_id: str) -> dict[str, Any]:
    """Announce that a peer went away (sender_id is the peer that left)."""
    return build(MSG_PEER_LEFT, left_id, peer_id=left_id)


def make_sync_ping(sender_id: str, t1: float) -> dict[str, Any]:
    return build(MSG_SYNC_PING, sender_id, t1=float(t1))


def make_sync_pong(host_id: str, t1: float, t2: float, t3: float) -> dict[str, Any]:
    return build(MSG_SYNC_PONG, host_id, t1=float(t1), t2=float(t2), t3=float(t3))


def make_resync_request(sender_id: str) -> dict[str, Any]:
    """Ask the host to make everyone follow its current position/state."""
    return build(MSG_RESYNC_REQUEST, sender_id)


def make_resync_apply(
    host_id: str, *, position: float, is_playing: bool
) -> dict[str, Any]:
    """The host's authoritative resync broadcast (always a fresh msg_id)."""
    return build(
        MSG_RESYNC_APPLY,
        host_id,
        position=round(max(0.0, as_float(position)), 3),
        is_playing=bool(is_playing),
        host_time=now(),
    )


# --- time resolution ---------------------------------------------------------

#: Ignore absurd skew values (clock jumps, bogus offsets). Anything in this
#: range is indistinguishable from real network/broadcast delay.
MAX_SKEW = 2.0


def _elapsed(host_time: Any, clock_offset: float, local_now: float) -> float:
    """Seconds elapsed since the host stamped *host_time* (never negative)."""
    if not isinstance(host_time, (int, float)) or isinstance(host_time, bool):
        return 0.0
    return max(0.0, (local_now + clock_offset) - float(host_time))


def _skew(host_time: Any, clock_offset: float, local_now: float) -> float:
    """Elapsed time clamped for position compensation.

    The clamp keeps a bad clock offset from throwing playback seconds away
    from where it was. Countdown math must NOT use this: a countdown message
    that arrives late still has to lose exactly the time it was in flight.
    """
    return min(_elapsed(host_time, clock_offset, local_now), MAX_SKEW)


def resolve_target(
    message: dict[str, Any],
    clock_offset: float = 0.0,
    local_now: float | None = None,
) -> tuple[float, bool]:
    """Turn an action message into the (position, is_playing) to apply.

    For a playing target, the position is advanced by the time that passed
    since the host sent the message, so every peer lands on the same media
    position even if delivery took a little longer for one of them.
    """
    reference = now() if local_now is None else local_now
    playing = bool(message.get("is_playing", message.get("type") == MSG_PLAY))
    position = max(0.0, as_float(message.get("position")))
    if playing:
        position += _skew(message.get("host_time"), clock_offset, reference)
    return position, playing


def resolve_state(
    state: dict[str, Any],
    clock_offset: float = 0.0,
    local_now: float | None = None,
) -> tuple[float, bool]:
    """Same as resolve_target, for the ``state`` block of a welcome."""
    reference = now() if local_now is None else local_now
    playing = bool(state.get("is_playing"))
    position = max(0.0, as_float(state.get("position")))
    if playing:
        position += _skew(state.get("target_time"), clock_offset, reference)
    return position, playing


def is_countdown(message: dict[str, Any]) -> bool:
    """True for a play message that asks for a 3-2-1 countdown."""
    return (
        message.get("type") == MSG_PLAY
        and as_float(message.get("start_in_seconds")) > 0.0
    )


def countdown_remaining(
    message: dict[str, Any],
    clock_offset: float = 0.0,
    local_now: float | None = None,
) -> float:
    """Countdown seconds left for a peer that receives *message* now.

    ``start_in_seconds`` was chosen on the requester's clock and stamped with
    the host's ``host_time`` at broadcast, so the remaining time is the field
    minus the time already elapsed since the host sent it. Every peer then
    unpauses at the same instant.
    """
    total = as_float(message.get("start_in_seconds"))
    if total <= 0.0:
        return 0.0
    reference = now() if local_now is None else local_now
    elapsed = _elapsed(message.get("host_time"), clock_offset, reference)
    return max(0.0, total - elapsed)


def resolve_countdown(
    message: dict[str, Any],
    clock_offset: float = 0.0,
    local_now: float | None = None,
) -> tuple[float, float]:
    """(position, remaining seconds) for a countdown play message.

    The position is deliberately not advanced by the skew: during the
    countdown everybody is parked on the same frame.
    """
    position = max(0.0, as_float(message.get("position")))
    return position, countdown_remaining(message, clock_offset, local_now)


# --- echo suppression --------------------------------------------------------


class MessageTracker:
    """Remembers msg_ids created locally so their host echo can be ignored.

    The host broadcasts every action back to its originator. That peer has
    already applied the action at key-press time, so applying the echo again
    would at best be a no-op and at worst cause a visible jump.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._capacity = max(8, capacity)
        self._ids: OrderedDict[str, None] = OrderedDict()

    def remember(self, msg_id: Any) -> None:
        if not isinstance(msg_id, str) or not msg_id:
            return
        self._ids[msg_id] = None
        self._ids.move_to_end(msg_id)
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)

    def issue(self, msg_type: str, sender_id: str, **fields: Any) -> dict[str, Any]:
        """Build a message and remember its id in one step."""
        message = build(msg_type, sender_id, **fields)
        self.remember(message["msg_id"])
        return message

    def is_echo(self, msg_id: Any) -> bool:
        return isinstance(msg_id, str) and msg_id in self._ids
