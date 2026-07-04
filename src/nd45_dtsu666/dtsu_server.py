"""DTSU666 RTU server: datastore build/update + fail-safe supervisor."""

from __future__ import annotations

import logging

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)

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
