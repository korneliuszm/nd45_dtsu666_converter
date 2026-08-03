"""Float32 <-> Modbus register codec with configurable word/byte order.

The ND45 speaks float32 throughout. The Huawei SmartLogger instead uses sized
integers (U16/I16/U32/I32/U64/I64) with a documented "Gain" divisor, so the
integer half below exists purely for that source. Huawei's Gain needs no new
concept: it folds into the existing `scale` multiplier (e.g. Active power is
I32 gain 1000 in kW, so SI watts = raw/1000 * 1000 -> scale 1.0).
"""

from __future__ import annotations

import math
import struct

OVERRANGE = 1e20  # ND45 writes 2e20 when a value is out of measuring range
FLOAT32_MAX = 3.4028234663852886e38

# dtype -> (register count, signed). float32 is handled by the float path above.
INT_DTYPES: dict[str, tuple[int, bool]] = {
    "u16": (1, False),
    "i16": (1, True),
    "u32": (2, False),
    "i32": (2, True),
    "u64": (4, False),
    "i64": (4, True),
}

DTYPES = frozenset({"float32", *INT_DTYPES})


def register_width(dtype: str) -> int:
    """Registers occupied by one point of this dtype."""
    if dtype == "float32":
        return 2
    try:
        return INT_DTYPES[dtype][0]
    except KeyError:
        raise ValueError(f"unknown dtype {dtype!r}") from None


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
    if not math.isfinite(value) or abs(value) > FLOAT32_MAX:
        raise ValueError(
            f"value is not representable as finite float32: {value!r}"
        )
    raw = struct.pack(">f", value)
    hi, lo = raw[0:2], raw[2:4]
    words = [hi, lo] if word_order == "big" else [lo, hi]
    return [_bytes_word(w, byte_order) for w in words]


def decode_point(
    regs: list[int], scale: float, sign: int, offset: float, word_order: str, byte_order: str
) -> float:
    raw = registers_to_float(regs, word_order, byte_order)
    return raw * scale * sign + offset


def encode_point(
    si: float,
    scale: float,
    sign: int,
    offset: float,
    word_order: str,
    byte_order: str,
    zero_low_word: bool = False,
) -> list[int]:
    register_float = si * sign * scale + offset
    registers = float_to_registers(register_float, word_order, byte_order)
    if zero_low_word:
        low_index = 1 if word_order == "big" else 0
        registers[low_index] = 0
    return registers


def compose(values: list[float], factors: list[float]) -> float:
    return sum(v * f for v, f in zip(values, factors))


def registers_to_int(
    regs: list[int], dtype: str, word_order: str = "big", byte_order: str = "big"
) -> int:
    """Assemble sized-integer register words into a Python int.

    Same ordering convention as the float path: each register is turned into
    bytes by `byte_order`, then the words are laid out most-significant-first
    for word_order "big" (reversed otherwise).
    """
    try:
        count, signed = INT_DTYPES[dtype]
    except KeyError:
        raise ValueError(f"{dtype!r} is not an integer dtype") from None
    if len(regs) < count:
        raise ValueError(f"dtype {dtype} needs {count} register(s), got {len(regs)}")
    words = list(regs[:count])
    if word_order != "big":
        words.reverse()
    raw = b"".join(_word_bytes(w, byte_order) for w in words)
    return int.from_bytes(raw, "big", signed=signed)


def int_sentinel(dtype: str) -> int:
    """The "value invalid" marker Huawei writes for an unavailable channel.

    The SmartLogger manual does not tabulate it per register, but the family
    convention is the type maximum (0x7FFF / 0xFFFF / 0x7FFFFFFF / ...), which
    is also outside any physical range for the quantities mapped here.
    """
    count, signed = INT_DTYPES[dtype]
    bits = count * 16
    return (1 << (bits - 1)) - 1 if signed else (1 << bits) - 1


def int_is_invalid(raw: int, dtype: str) -> bool:
    return raw == int_sentinel(dtype)


def decode_int_point(
    regs: list[int],
    dtype: str,
    scale: float,
    sign: int,
    offset: float,
    word_order: str,
    byte_order: str,
) -> float:
    """Decode a sized integer into SI, returning NaN for the invalid sentinel.

    NaN (rather than a separate return channel) so callers reuse the same
    `math.isfinite` validity check the float32/ND45 path already applies.
    """
    raw = registers_to_int(regs, dtype, word_order, byte_order)
    if int_is_invalid(raw, dtype):
        return math.nan
    return raw * scale * sign + offset
