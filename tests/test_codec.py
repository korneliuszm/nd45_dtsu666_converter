import math

import pytest

import nd45_dtsu666.codec as codec

from nd45_dtsu666.codec import (
    INT_DTYPES,
    compose,
    decode_int_point,
    decode_point,
    encode_point,
    float_to_registers,
    int_is_invalid,
    int_sentinel,
    register_width,
    registers_to_float,
    registers_to_int,
)


def test_float_to_registers_big_big_known_value():
    # 230.0 -> IEEE754 big-endian = 0x43660000 -> [0x4366, 0x0000]
    assert float_to_registers(230.0, "big", "big") == [0x4366, 0x0000]


def test_registers_to_float_big_big_known_value():
    assert registers_to_float([0x4366, 0x0000], "big", "big") == pytest.approx(230.0)


def test_roundtrip_all_orders():
    for wo in ("big", "little"):
        for bo in ("big", "little"):
            regs = float_to_registers(123.456, wo, bo)
            assert registers_to_float(regs, wo, bo) == pytest.approx(123.456, rel=1e-6)


def test_word_swap_changes_bytes():
    assert float_to_registers(230.0, "big", "big") != float_to_registers(230.0, "little", "big")


def test_decode_point_applies_scale_sign_offset():
    regs = float_to_registers(100.0, "big", "big")
    # SI = raw * scale * sign + offset
    assert decode_point(regs, scale=2.0, sign=-1, offset=5.0, word_order="big", byte_order="big") == pytest.approx(-195.0)


def test_encode_point_applies_sign_scale_offset():
    # register_float = SI * sign * scale + offset ; DTSU voltage scale x10
    regs = encode_point(230.0, scale=10.0, sign=1, offset=0.0, word_order="big", byte_order="big")
    assert registers_to_float(regs, "big", "big") == pytest.approx(2300.0)


def test_encode_decode_sign_inversion_for_power():
    regs = encode_point(-1500.0, scale=10.0, sign=-1, offset=0.0, word_order="big", byte_order="big")
    assert registers_to_float(regs, "big", "big") == pytest.approx(15000.0)


def test_compose_energy_high_low():
    # MWh part=2, kWh part=345 -> 2*1000 + 345 = 2345 kWh
    assert compose([2.0, 345.0], [1000.0, 1.0]) == pytest.approx(2345.0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_float_to_registers_rejects_non_finite(
    value, word_order, byte_order
):
    with pytest.raises(ValueError, match="finite float32"):
        float_to_registers(value, word_order, byte_order)


@pytest.mark.parametrize("value", [1e39, -1e39])
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_float_to_registers_rejects_finite_float32_overflow(
    value, word_order, byte_order
):
    with pytest.raises(ValueError, match="finite float32"):
        float_to_registers(value, word_order, byte_order)


def test_float_to_registers_accepts_float32_max():
    regs = float_to_registers(codec.FLOAT32_MAX)
    assert registers_to_float(regs) == codec.FLOAT32_MAX


def test_encode_point_rejects_overflow_created_by_scaling():
    with pytest.raises(ValueError, match="finite float32"):
        encode_point(
            1e38,
            scale=10.0,
            sign=1,
            offset=0.0,
            word_order="big",
            byte_order="big",
        )


@pytest.mark.parametrize(
    ("word_order", "low_index"),
    [("big", 1), ("little", 0)],
)
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_encode_point_can_zero_logical_low_word(
    word_order, low_index, byte_order
):
    regs = encode_point(
        12.345,
        scale=1.0,
        sign=1,
        offset=0.0,
        word_order=word_order,
        byte_order=byte_order,
        zero_low_word=True,
    )

    assert regs[low_index] == 0
    assert regs[1 - low_index] != 0


def test_encode_point_preserves_low_word_by_default():
    assert encode_point(
        12.345,
        scale=1.0,
        sign=1,
        offset=0.0,
        word_order="big",
        byte_order="big",
    ) == float_to_registers(12.345, "big", "big")


# --- sized integers (Huawei SmartLogger) -------------------------------------


@pytest.mark.parametrize(
    "dtype,value",
    [
        ("u16", 0),
        ("u16", 65535),
        ("i16", -32768),
        ("i16", 32767),
        ("u32", 0),
        ("u32", 4294967295),
        ("i32", -2147483648),
        ("i32", 2147483647),
        ("u64", 18446744073709551615),
        ("i64", -9223372036854775808),
        ("i64", 9223372036854775807),
    ],
)
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize("byte_order", ["big", "little"])
def test_registers_to_int_roundtrip_all_orders(dtype, value, word_order, byte_order):
    """Encode with the same convention the decoder claims, then read it back."""
    count, signed = INT_DTYPES[dtype]
    raw = value.to_bytes(count * 2, "big", signed=signed)
    words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
    if word_order != "big":
        words.reverse()
    if byte_order != "big":
        words = [((w & 0xFF) << 8) | (w >> 8) for w in words]

    assert registers_to_int(words, dtype, word_order, byte_order) == value


def test_registers_to_int_known_values_big_big():
    assert registers_to_int([0x1234], "u16", "big", "big") == 0x1234
    assert registers_to_int([0xFFFF], "i16", "big", "big") == -1
    assert registers_to_int([0x0012, 0x3456], "u32", "big", "big") == 0x00123456
    assert registers_to_int([0xFFFF, 0xFFFF], "i32", "big", "big") == -1
    assert registers_to_int([0, 0, 0, 5], "i64", "big", "big") == 5


def test_register_width_matches_dtype():
    assert register_width("float32") == 2
    assert register_width("u16") == 1
    assert register_width("i16") == 1
    assert register_width("u32") == 2
    assert register_width("i32") == 2
    assert register_width("i64") == 4
    with pytest.raises(ValueError, match="unknown dtype"):
        register_width("i8")


def test_registers_to_int_rejects_a_short_slice():
    with pytest.raises(ValueError, match="needs 2 register"):
        registers_to_int([0x1234], "u32", "big", "big")


def test_registers_to_int_rejects_a_float_dtype():
    with pytest.raises(ValueError, match="not an integer dtype"):
        registers_to_int([0, 0], "float32", "big", "big")


def test_int_sentinel_is_the_type_maximum():
    assert int_sentinel("u16") == 0xFFFF
    assert int_sentinel("i16") == 0x7FFF
    assert int_sentinel("u32") == 0xFFFFFFFF
    assert int_sentinel("i32") == 0x7FFFFFFF
    assert int_sentinel("i64") == 0x7FFFFFFFFFFFFFFF
    assert int_is_invalid(0x7FFF, "i16") is True
    assert int_is_invalid(0x7FFE, "i16") is False


def test_decode_int_point_applies_scale_sign_offset():
    # Huawei "Gain 1000" on a kW register folds into scale 1.0 for SI watts
    assert decode_int_point(
        [0x0012, 0xD687], "i32", 1.0, 1, 0.0, "big", "big"
    ) == pytest.approx(1234567.0)
    # Gain 10 on a volt register -> scale 0.1
    assert decode_int_point([4001], "u16", 0.1, 1, 0.0, "big", "big") == pytest.approx(400.1)
    # sign flip and offset still apply, as on the float path
    assert decode_int_point([100], "i16", 2.0, -1, 5.0, "big", "big") == pytest.approx(-195.0)


def test_decode_int_point_returns_nan_for_the_invalid_sentinel():
    """NaN so callers reuse the same math.isfinite check the float path uses."""
    assert math.isnan(decode_int_point([0x7FFF], "i16", 0.001, 1, 0.0, "big", "big"))
    assert math.isnan(
        decode_int_point([0xFFFF, 0xFFFF], "u32", 1.0, 1, 0.0, "big", "big")
    )
