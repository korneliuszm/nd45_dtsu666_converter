import pytest

import nd45_dtsu666.codec as codec

from nd45_dtsu666.codec import (
    compose,
    decode_point,
    encode_point,
    float_to_registers,
    registers_to_float,
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
