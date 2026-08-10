"""Read a source through modbus-hub instead of opening our own TCP client.

The hub (korneliuszm/modbus-hub) is the device's single owner of every Modbus
link, so two applications sharing a SmartLogger cost one poll instead of two and
nothing can interleave frames on a bus. This module is the client side.

The seam it plugs into already exists: `app._make_client_factory` is the ONLY
place a Modbus client is constructed, and everything downstream -- all three
`poll_once` implementations, `run_poller`, `connect_with_retry`,
`supervise_poller`, `diagnostics` -- duck-types the client as

    connect() -> bool | close() | connected
    read_holding_registers(addr, count, slave=) -> obj with .isError()/.registers
    read_input_registers(addr, count, slave=)   -> ditto

`etango_poller.MultiHostClient` already proves that shape is an interface rather
than a class. `HubClient` satisfies the same contract, so the pollers, the codec,
the canonical store and the DTSU output server are all untouched.

Two things about this client are load-bearing:

* **Reads never go to the network.** The hub *pushes* blocks over a Subscribe
  stream and this client answers from what it last received. That is what lets
  the ND45 bridge keep its 0.05 s cadence without a round trip per poll.

* **A block that is missing, failed or stale answers `isError()`.** It must NEVER
  fall back to the previously received registers. `poll_once` turns that error
  into `PollError`, the `CanonicalStore` stops advancing, `HealthGate` trips and
  `supervise_server` silences the DTSU output so Sigenergy enters its own safe
  mode. Substituting a stale value would leave Sigenergy regulating against a
  frozen meter, with nothing anywhere reporting a fault.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .config import EtangoSourceConf, SourceSide

log = logging.getLogger(__name__)

__all__ = [
    "BlockCache",
    "BlockKey",
    "HubClient",
    "HubConnection",
    "HubResponse",
    "blocks_for_source",
    "make_hub_client",
    "staleness_threshold",
]

# Modbus function codes the hub serves.
FC_HOLDING = 3
FC_INPUT = 4


@dataclass(frozen=True)
class BlockKey:
    """One contiguous register range on one device behind one hub link."""

    link: str
    unit_id: int
    fc: int
    base: int
    count: int

    def covers(self, link: str, unit_id: int, fc: int, addr: int, count: int) -> bool:
        return (
            self.link == link
            and self.unit_id == unit_id
            and self.fc == fc
            and addr >= self.base
            and addr + count <= self.base + self.count
        )


class HubResponse:
    """Stands in for a pymodbus response object.

    The pollers only ever call `.isError()` and read `.registers`, and print the
    object itself into the error message, so those three are the whole contract.
    """

    __slots__ = ("registers", "_error")

    def __init__(self, registers: list[int] | None = None, error: str | None = None) -> None:
        self.registers: list[int] = registers or []
        self._error = error

    def isError(self) -> bool:  # noqa: N802 - pymodbus spelling, matched deliberately
        return self._error is not None

    @property
    def error(self) -> str | None:
        return self._error

    def __str__(self) -> str:
        return self._error if self._error is not None else f"registers={self.registers}"

    def __repr__(self) -> str:
        return f"HubResponse({self})"


@dataclass
class _Entry:
    registers: list[int] | None
    # Local monotonic clock at the moment the update was RECEIVED, not the
    # hub's own read timestamp. The hub reports wall-clock nanoseconds; over a
    # unix socket the two differ by well under a millisecond, and a local
    # monotonic reading cannot be distorted by an NTP step the way a
    # cross-process wall-clock comparison can.
    received_at: float
    error: str | None = None


class BlockCache:
    """Last update per subscribed block, plus the staleness rule.

    Pure and synchronous on purpose: this is where the fail-safe decision lives,
    so it is testable without grpcio, without a socket and without a hub.
    """

    def __init__(self, max_stale_s: float, now=time.monotonic) -> None:
        self._entries: dict[BlockKey, _Entry] = {}
        self._max_stale_s = max_stale_s
        self._now = now
        # Set while the Subscribe stream is down. Every lookup then fails, even
        # one whose last value is still nominally young: a stream that is gone
        # is not going to refresh anything, and pretending otherwise only delays
        # the fail-safe by the staleness window.
        self._stream_error: str | None = "not connected yet"

    @property
    def stream_error(self) -> str | None:
        return self._stream_error

    def set_stream_error(self, error: str | None) -> None:
        self._stream_error = error

    def update(
        self, key: BlockKey, registers: list[int] | None, error: str | None = None
    ) -> None:
        self._entries[key] = _Entry(
            registers=None if error else list(registers or []),
            received_at=self._now(),
            error=error,
        )

    def lookup(self, link: str, unit_id: int, fc: int, addr: int, count: int) -> HubResponse:
        if self._stream_error is not None:
            return HubResponse(error=f"modbus-hub stream down: {self._stream_error}")

        best: tuple[BlockKey, _Entry] | None = None
        for key, entry in self._entries.items():
            if not key.covers(link, unit_id, fc, addr, count):
                continue
            if best is None or entry.received_at > best[1].received_at:
                best = (key, entry)

        if best is None:
            return HubResponse(
                error=(
                    f"no subscribed block covers link {link!r} unit {unit_id} "
                    f"fc {fc} addr {addr} count {count}"
                )
            )

        key, entry = best
        if entry.error:
            return HubResponse(error=f"modbus-hub reported: {entry.error}")

        age = self._now() - entry.received_at
        if age > self._max_stale_s:
            return HubResponse(
                error=(
                    f"stale block link {link!r} unit {unit_id} fc {fc} "
                    f"addr {key.base} count {key.count}: {age:.2f}s old "
                    f"(limit {self._max_stale_s:.2f}s)"
                )
            )

        assert entry.registers is not None  # guaranteed by update()
        offset = addr - key.base
        return HubResponse(registers=entry.registers[offset:offset + count])


def staleness_threshold(poll_interval_s: float, timeout_s: float) -> float:
    """How old a pushed block may get before the bridge treats it as failed.

    Wide enough that ordinary jitter (one missed cycle plus the device's own
    timeout) does not trip it, tight enough that it always fires before the
    bridge's `safety.max_data_age_s` would otherwise silently pass. As deployed:
    ND45 1.0s (max_data_age 3.0s), eTango 3.0s (3.0s), SmartLogger 6.0s (30.0s).
    """
    return max(timeout_s, poll_interval_s * 3.0)


def blocks_for_source(
    source_type: str, source_side: SourceSide, unit_id: int, link: str
) -> list[BlockKey]:
    """Which blocks this source needs, exactly as they go on the wire.

    The pollers call `read_*_registers(wire_base, count, slave=unit)`, so the
    subscription has to be declared in the same terms -- documented base plus
    `address_offset`, and the per-group unit override where the map has one.
    """
    if source_type == "nd45":
        # The ND45 poller uses a module constant rather than registers.json.
        from .nd45_poller import READ_GROUPS

        return [
            BlockKey(link=link, unit_id=unit_id, fc=FC_HOLDING, base=base, count=count)
            for base, count in READ_GROUPS
        ]

    fc = FC_INPUT if source_type == "etango" else FC_HOLDING
    blocks: list[BlockKey] = []
    for group in source_side.read_groups or []:
        unit = group.unit_id if group.unit_id is not None else unit_id
        blocks.append(
            BlockKey(
                link=link,
                unit_id=unit,
                fc=fc,
                base=group.base + source_side.address_offset,
                count=group.count,
            )
        )
    if not blocks:
        raise ValueError(
            f"source type {source_type!r} declares no read_groups; "
            "the hub would be asked to poll nothing"
        )
    return blocks


class HubConnection:
    """One gRPC channel and one Subscribe stream, shared by its `HubClient` views.

    A multi-host source (eTango) becomes several links on ONE stream rather than
    several connections: the hub already merges everybody's blocks, and a single
    stream keeps reconnect handling in one place.

    grpcio is imported lazily so the pure parts of this module -- and the whole
    existing test suite -- keep working in an environment without it.
    """

    def __init__(
        self,
        socket_path: str,
        client_id: str,
        blocks: list[BlockKey],
        period_s: float,
        max_stale_s: float,
        reconnect_delay_s: float = 1.0,
        reconnect_delay_max_s: float = 30.0,
    ) -> None:
        self.socket_path = socket_path
        self.client_id = client_id
        self.blocks = blocks
        self.period_s = period_s
        self.cache = BlockCache(max_stale_s)
        self._reconnect_delay_s = reconnect_delay_s
        self._reconnect_delay_max_s = reconnect_delay_max_s

        self._channel = None
        self._task: asyncio.Task | None = None
        self._closed = False
        # Readiness means EVERY declared block has been seen at least once, not
        # just the first one to arrive. The blocks land as separate stream
        # messages a few milliseconds apart, so signalling on the first would
        # let the very next poll_once fail on a block that simply had not been
        # delivered yet -- which is a startup race, not a fault worth failing.
        self._ready = asyncio.Event()
        self._awaited: set[BlockKey] = set(blocks)

    @property
    def connected(self) -> bool:
        return self.cache.stream_error is None

    async def connect(self) -> bool:
        """Start (or keep) the stream; True once a first update has arrived.

        `connect_with_retry` loops on this, so returning False is a normal
        "not yet" rather than an error.
        """
        if self._closed:
            return False
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.period_s * 5 + 2.0)
        except asyncio.TimeoutError:
            return False
        return self.connected

    def close(self) -> None:
        """Idempotent: a MultiHostClient closes each of its links in turn, and
        several of them share one connection."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        channel, self._channel = self._channel, None
        if channel is not None:
            # close() is a coroutine on grpc.aio; fire and forget, we are in a
            # sync context that mirrors pymodbus's sync close().
            try:
                asyncio.get_running_loop().create_task(channel.close())
            except RuntimeError:  # no running loop (shutdown path)
                pass
        self.cache.set_stream_error("closed")

    # -- reads ------------------------------------------------------------

    def read(self, link: str, fc: int, addr: int, count: int, slave: int) -> HubResponse:
        return self.cache.lookup(link, slave, fc, addr, count)

    # -- stream supervision -----------------------------------------------

    async def _run(self) -> None:
        delay = self._reconnect_delay_s
        while not self._closed:
            try:
                await self._stream_once()
                delay = self._reconnect_delay_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure means retry
                self.cache.set_stream_error(str(exc))
                log.warning(
                    "modbus-hub stream failed (%s); retrying in %.1fs", exc, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_delay_max_s)

    async def _stream_once(self) -> None:
        import grpc  # noqa: PLC0415 - lazy so grpcio stays optional at import time

        from .hubpb import hub_pb2, hub_pb2_grpc

        if self._channel is None:
            self._channel = grpc.aio.insecure_channel(f"unix://{self.socket_path}")
        stub = hub_pb2_grpc.ModbusHubStub(self._channel)

        request = hub_pb2.SubscribeRequest(
            client_id=self.client_id,
            blocks=[
                hub_pb2.BlockSubscription(
                    block=hub_pb2.BlockRef(
                        link=b.link,
                        unit_id=b.unit_id,
                        function_code=b.fc,
                        base=b.base,
                        count=b.count,
                    ),
                    period_ms=max(1, int(self.period_s * 1000)),
                )
                for b in self.blocks
            ],
        )

        stream = stub.Subscribe()
        await stream.write(request)
        log.info(
            "modbus-hub: subscribed %d block(s) as %r at %.0f ms",
            len(self.blocks), self.client_id, self.period_s * 1000,
        )

        async for update in stream:
            ref = update.block
            key = BlockKey(
                link=ref.link,
                unit_id=ref.unit_id,
                fc=ref.function_code,
                base=ref.base,
                count=ref.count,
            )
            if update.error:
                self.cache.update(key, None, error=update.error)
            else:
                self.cache.update(key, list(update.registers))
            self.cache.set_stream_error(None)
            # A block that keeps failing still counts as delivered: the caller
            # gets that error from lookup(), which is more useful than hanging
            # in connect() forever.
            self._awaited.discard(key)
            if not self._awaited:
                self._ready.set()

        raise ConnectionError("modbus-hub closed the Subscribe stream")


@dataclass
class HubClient:
    """A single-link view of a `HubConnection`, shaped like a pymodbus client."""

    conn: HubConnection
    link: str
    # For error messages only, mirroring etango_poller.DeviceLink.host.
    label: str = ""

    async def connect(self) -> bool:
        return await self.conn.connect()

    def close(self) -> None:
        self.conn.close()

    @property
    def connected(self) -> bool:
        return self.conn.connected

    async def read_holding_registers(self, address, count=1, slave=0, **_kwargs):
        return self.conn.read(self.link, FC_HOLDING, address, count, slave)

    async def read_input_registers(self, address, count=1, slave=0, **_kwargs):
        return self.conn.read(self.link, FC_INPUT, address, count, slave)


def make_hub_client(spec, source_side: SourceSide):
    """Build the hub-backed client for one bridge.

    Returns an object satisfying the same duck type as
    `AsyncModbusTcpClient` (single-host sources) or
    `etango_poller.MultiHostClient` (multi-host), so `app.build_bridge`,
    `connect_with_retry`, `supervise_poller` and `diagnostics` need no branch
    beyond the one in `_make_client_factory`.
    """
    source = spec.source
    max_stale_s = staleness_threshold(source.poll_interval_s, source.timeout_s)

    if isinstance(source, EtangoSourceConf):
        from .etango_poller import DeviceLink, MultiHostClient

        blocks: list[BlockKey] = []
        for device in source.devices:
            if not device.hub_link:
                raise ValueError(
                    f"bridge {spec.name!r}: etango device {device.host} has via_hub set "
                    "but no hub_link; each device needs the hub link name that owns it"
                )
            blocks.extend(
                blocks_for_source("etango", source_side, device.unit_id, device.hub_link)
            )

        conn = HubConnection(
            socket_path=source.hub_socket,
            client_id=f"nd45-dtsu666/{spec.name}",
            blocks=blocks,
            period_s=source.poll_interval_s,
            max_stale_s=max_stale_s,
            reconnect_delay_s=source.reconnect_delay_s,
            reconnect_delay_max_s=source.reconnect_delay_max_s,
        )
        return MultiHostClient([
            DeviceLink(
                client=HubClient(conn=conn, link=device.hub_link, label=device.host),
                unit_id=device.unit_id,
                aggregate=device.aggregate,
                host=device.host,
            )
            for device in source.devices
        ])

    if not source.hub_link:
        raise ValueError(
            f"bridge {spec.name!r}: via_hub is set but hub_link is empty; "
            "it must name the link in the hub's own config that owns this device"
        )

    blocks = blocks_for_source(source.type, source_side, source.unit_id, source.hub_link)
    conn = HubConnection(
        socket_path=source.hub_socket,
        client_id=f"nd45-dtsu666/{spec.name}",
        blocks=blocks,
        period_s=source.poll_interval_s,
        max_stale_s=max_stale_s,
        reconnect_delay_s=source.reconnect_delay_s,
        reconnect_delay_max_s=source.reconnect_delay_max_s,
    )
    return HubClient(conn=conn, link=source.hub_link, label=source.hub_link)
