"""DTSU666 RTU server: datastore build/update + fail-safe supervisor."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.framer.rtu_framer import ModbusRtuFramer
from pymodbus.server import ModbusSerialServer

from .codec import encode_point
from .config import TargetSide

log = logging.getLogger(__name__)

_HOLDING_FC = 3


def build_context(target: TargetSide, slave_id: int) -> ModbusServerContext:
    max_addr = max(pt.addr for pt in target.points.values())
    block = ModbusSequentialDataBlock(0, [0] * (max_addr + 2))
    slave = ModbusSlaveContext(hr=block, zero_mode=True)
    return ModbusServerContext(slaves={slave_id: slave}, single=False)


def update_datastore(
    context: ModbusServerContext, slave_id: int, canonical: dict[str, float], target: TargetSide
) -> None:
    slave = context[slave_id]
    wo, bo = target.word_order, target.byte_order
    for pt in target.points.values():
        si = canonical.get(pt.from_)
        if si is None:
            continue
        regs = encode_point(si, pt.scale, pt.sign, pt.offset, wo, bo)
        slave.setValues(_HOLDING_FC, pt.addr, regs)


def make_serial_server(cfg, context) -> ModbusSerialServer:
    return ModbusSerialServer(
        context=context,
        framer=ModbusRtuFramer,
        port=cfg.port,
        baudrate=cfg.baudrate,
        parity=cfg.parity,
        stopbits=cfg.stopbits,
        bytesize=8,
    )


async def supervise_server(
    cfg,
    context,
    store,
    gate,
    check_interval: float,
    stop_event: asyncio.Event,
    server_factory: Callable | None = None,
    now: Callable[[], float] | None = None,
) -> None:
    """Start the RTU server while data is fresh; stop it (silence) while stale."""
    factory = server_factory or (lambda: make_serial_server(cfg, context))
    clock = now or time.monotonic
    server = None
    serve_task: asyncio.Task | None = None

    try:
        while not stop_event.is_set():
            age = store.age(clock())
            if gate.should_serve(age) and server is None:
                server = factory()
                serve_task = asyncio.create_task(server.serve_forever())
                log.info("RTU server started (data fresh, age=%.2fs)", age)
            elif not gate.should_serve(age) and server is not None:
                await server.shutdown()
                if serve_task:
                    serve_task.cancel()
                server, serve_task = None, None
                log.warning("RTU server stopped (data stale, age=%.2fs) -> fail-safe", age)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        if server is not None:
            await server.shutdown()
            if serve_task:
                serve_task.cancel()
