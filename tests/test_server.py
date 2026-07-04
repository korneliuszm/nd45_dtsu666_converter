import pytest

from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_registers
from nd45_dtsu666.dtsu_server import build_context, update_datastore


def _read_point(context, slave_id, pt, target):
    # Inverse of encode_point (raw = si*sign*scale + offset): recover SI from the
    # DTSU register. decode_point cannot be used here because it multiplies by
    # scale (source-side convention), while the target side stores si*scale.
    regs = context[slave_id].getValues(3, pt.addr, count=2)
    raw = registers_to_float(regs, target.word_order, target.byte_order)
    return (raw - pt.offset) / (pt.scale * pt.sign)


def test_update_datastore_encodes_voltage_and_power():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {"u_l1": 230.0, "p_total": 1500.0}, target)

    u = _read_point(context, 1, target.points["u_l1"], target)
    p = _read_point(context, 1, target.points["p_total"], target)
    assert u == pytest.approx(230.0, rel=1e-5)
    assert p == pytest.approx(1500.0, rel=1e-5)


def test_datastore_raw_scaling_matches_dtsu_spec():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {"u_l1": 230.0}, target)
    # DTSU voltage is x10 -> raw register float must be 2300.0
    from nd45_dtsu666.codec import registers_to_float

    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == pytest.approx(2300.0)


def test_missing_canonical_key_is_skipped():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {}, target)  # no values -> no crash
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert regs == [0, 0]
