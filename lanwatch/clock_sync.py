"""NTP-like clock offset estimation between a LANWatch client and its host.

The client sends ``sync_ping`` with t1 and the host immediately answers
``sync_pong`` with t1, t2 (host receive time) and t3 (host send time). The
client stamps t4 on arrival and computes

    offset = ((t2 - t1) + (t3 - t4)) / 2

which is the host clock minus the client clock, minus the (symmetric) network
delay. Repeating the round a few times and taking the median makes a single
slow packet harmless.

All timestamps are ``time.perf_counter()`` values. Only differences are ever
used, so it does not matter that the two machines' counters start at arbitrary
points.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import config
import protocol
from protocol import now

#: A ping round: send a ping for *t1* and resolve with the matching pong.
PingFn = Callable[[float], Awaitable[dict[str, Any]]]


class ClockSyncError(RuntimeError):
    """Raised when no clock sample could be measured at all."""


@dataclass(slots=True, frozen=True)
class ClockSample:
    """One round trip's result."""

    offset: float
    rtt: float


class ClockSync:
    """Measures and stores the host-minus-client clock offset."""

    def __init__(
        self,
        rounds: int = config.CLOCK_SYNC_ROUNDS,
        timeout: float = config.CLOCK_SYNC_TIMEOUT,
    ) -> None:
        self.rounds = max(1, rounds)
        self.timeout = timeout
        self.offset = 0.0
        self.rtt = 0.0
        self.samples: list[ClockSample] = []

    async def measure(self, ping: PingFn) -> ClockSample:
        """Run the rounds and adopt the median offset.

        Raises ClockSyncError if every round failed, because a client without
        an offset cannot place commands in time.
        """
        self.samples.clear()
        best: ClockSample | None = None

        for attempt in range(self.rounds):
            t1 = now()
            try:
                pong = await asyncio.wait_for(ping(t1), timeout=self.timeout)
            except (asyncio.TimeoutError, OSError, ValueError, ClockSyncError):
                continue

            t4 = now()
            if not isinstance(pong, dict):
                continue
            t2 = pong.get("t2")
            t3 = pong.get("t3")
            if not isinstance(t2, (int, float)) or not isinstance(t3, (int, float)):
                continue

            offset = ((float(t2) - t1) + (float(t3) - t4)) / 2.0
            # Round trip minus the host's own processing time.
            rtt = max(0.0, (t4 - t1) - (float(t3) - float(t2)))
            sample = ClockSample(offset=offset, rtt=rtt)
            self.samples.append(sample)
            if best is None or sample.rtt < best.rtt:
                best = sample

            if attempt + 1 < self.rounds:
                await asyncio.sleep(0.05)

        if not self.samples or best is None:
            raise ClockSyncError("host did not answer any clock sync ping")

        self.offset = statistics.median(sample.offset for sample in self.samples)
        # The lowest-RTT sample is the least delay-biased one.
        self.rtt = best.rtt
        return ClockSample(offset=self.offset, rtt=self.rtt)

    def host_to_local(self, host_time: float) -> float:
        """Convert a host monotonic timestamp into this machine's clock."""
        return host_time - self.offset

    def local_to_host(self, local_time: float) -> float:
        """Convert this machine's monotonic clock into the host's clock."""
        return local_time + self.offset

    def reset(self) -> None:
        self.offset = 0.0
        self.rtt = 0.0
        self.samples.clear()


def answer_ping(host_id: str, ping: dict[str, Any]) -> dict[str, Any]:
    """Host side of a ping: stamp t2/t3 and return the pong message.

    Kept here next to the estimator so both halves of the algorithm live in one
    file. t2 and t3 are taken back to back: the host has nothing to do between
    receiving and answering.
    """
    t2 = now()
    t1 = ping.get("t1")
    if not isinstance(t1, (int, float)):
        t1 = t2
    return protocol.make_sync_pong(host_id, float(t1), t2, now())
