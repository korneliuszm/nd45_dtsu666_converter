"""Float32 <-> Modbus register codec with configurable word/byte order."""

from __future__ import annotations

import math
import struct

OVERRANGE = 1e20  # ND45 writes 2e20 when a value is out of measuring range


def _word_bytes(reg: int, byte_order: str) -> bytes:
    if byte_order == "big":
        return bytes([(reg >> 8) & 0xFF, reg & 0xFF])
    return bytes([reg & 0xFF, (reg >> 8) & 0xFF])


def _bytes_word(pair: bytes, byte_order: str) -> int:
    if byte_order == "big":
        return (pair[0] << 8) | pair[1]
    return (pair[1] << 8) | pair[0]


def registers_to_float(regs: list[int], word_order: str = "big", byte_order: str = "big") -> float:
    words = [_word_bytes(r, byte_order) for r in regs]
    raw = words[0] + words[1] if word_order == "big" else words[1] + words[0]
    return struct.unpack(">f", raw)[0]


def float_to_registers(value: float, word_order: str = "big", byte_order: str = "big") -> list[int]:
    try:
        raw = struct.pack(">f", value)
    except OverflowError:
        # A finite value beyond float32's range: struct raises instead of
        # saturating, so saturate ourselves to the same-signed IEEE-754
        # infinity. Encoding must never raise -- otherwise one bad point would
        # abort a whole datastore update mid-write. This only backstops
        # pathological configured/debug inputs; live readings are clamped in
        # poll_once, and masking non-finite values stays the poller's job (the
        # codec carries IEEE-754 specials faithfully, see test_codec).
        raw = struct.pack(">f", math.copysign(math.inf, value))
    hi, lo = raw[0:2], raw[2:4]
    words = [hi, lo] if word_order == "big" else [lo, hi]
    return [_bytes_word(w, byte_order) for w in words]


def decode_point(
    regs: list[int], scale: float, sign: int, offset: float, word_order: str, byte_order: str
) -> float:
    raw = registers_to_float(regs, word_order, byte_order)
    return raw * scale * sign + offset


def encode_point(
    si: float, scale: float, sign: int, offset: float, word_order: str, byte_order: str
) -> list[int]:
    register_float = si * sign * scale + offset
    return float_to_registers(register_float, word_order, byte_order)


def compose(values: list[float], factors: list[float]) -> float:
    return sum(v * f for v, f in zip(values, factors))
