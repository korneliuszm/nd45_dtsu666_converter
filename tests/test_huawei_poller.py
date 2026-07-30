"""Huawei SmartLogger source: integer decoding, unit-id routing, derive, faults.

No real socket is opened -- the SmartLogger is stood in for by a duck-typed fake
client serving a synthetic integer register image, mirroring the ND45 pattern in
test_poller.py.
"""

from __future__ import annotations

import math

import pytest

from nd45_dtsu666 import huawei_poller
from nd45_dtsu666.codec import INT_DTYPES, int_sentinel
from nd45_dtsu666.config import (
    AppConfig,
    RegisterMap,
    SourceSide,
    load_config,
    load_registers,
)

REGISTERS = load_registers("config/registers.json")
CONFIG = load_config("config/config.json")


def _regs(value: int, dtype: str) -> list[int]:
    """Encode an integer into big/big register words (the maps' declared order)."""
    count, signed = INT_DTYPES[dtype]
    raw = value.to_bytes(count * 2, "big", signed=signed)
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


class FakeResponse:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class FakeClient:
    """Serves a synthetic SmartLogger image and records every request."""

    def __init__(self, image: dict[int, list[int]]):
        self.flat: dict[int, int] = {}
        for base, regs in image.items():
            for offset, reg in enumerate(regs):
                self.flat[base + offset] = reg
        self.requests: list[tuple[int, int, int]] = []

    async def read_holding_registers(self, address, count, slave=0):
        self.requests.append((address, count, slave))
        return FakeResponse([self.flat.get(a, 0) for a in range(address, address + count)])


# --- plant (logic device 0) --------------------------------------------------

PLANT_RAW = {
    40521: ("u32", 1_250_000),   # Input power, gain 1000 kW -> W
    40525: ("i32", 1_234_500),   # Active power, gain 1000 kW -> W
    40532: ("i16", 999),         # Power factor, gain 1000
    40544: ("i32", -50_000),     # Reactive power, gain 1000 kVar -> var
    40560: ("u32", 123_456),     # E-Total, gain 10 kWh
    40562: ("u32", 4_567),       # E-Daily, gain 10 kWh
    40572: ("i16", 100),         # Phase A current, gain 1
    40573: ("i16", 101),
    40574: ("i16", 102),
    40575: ("u16", 4_001),       # Uab, gain 10
    40576: ("u16", 4_002),
    40577: ("u16", 4_003),
}


def _plant_image(overrides: dict[int, tuple[str, int]] | None = None) -> dict[int, list[int]]:
    raw = {**PLANT_RAW, **(overrides or {})}
    return {addr: _regs(value, dtype) for addr, (dtype, value) in raw.items()}


async def _poll_plant(overrides=None, **kwargs):
    client = FakeClient(_plant_image(overrides))
    sources = [("huawei_plant", REGISTERS.huawei_plant_source, 0)]
    values = await huawei_poller.poll_once(client, sources, **kwargs)
    return client, values


async def test_plant_decodes_documented_registers():
    _client, v = await _poll_plant()
    assert v["pv_p_total"] == pytest.approx(1_234_500.0)
    assert v["pv_q_total"] == pytest.approx(-50_000.0)
    assert v["pv_pf_total"] == pytest.approx(0.999)
    assert v["pv_u_l12"] == pytest.approx(400.1)
    assert v["pv_u_l23"] == pytest.approx(400.2)
    assert v["pv_u_l31"] == pytest.approx(400.3)
    assert v["pv_i_l1"] == pytest.approx(100.0)
    assert v["pv_i_l3"] == pytest.approx(102.0)
    assert v["pv_exp_energy_total"] == pytest.approx(12_345.6)
    assert v["pv_e_daily"] == pytest.approx(456.7)
    assert v["pv_dc_power"] == pytest.approx(1_250_000.0)


async def test_plant_derives_everything_the_smartlogger_omits():
    _client, v = await _poll_plant()
    # phase voltages: the plant table exposes line voltages only
    assert v["pv_u_l1"] == pytest.approx(400.1 / math.sqrt(3.0))
    # per-phase power: only totals are published
    assert v["pv_p_l1"] == pytest.approx(1_234_500.0 / 3)
    assert v["pv_q_l2"] == pytest.approx(-50_000.0 / 3)
    # apparent power exists nowhere in the plant table
    assert v["pv_s_total"] == pytest.approx(math.hypot(1_234_500.0, -50_000.0))
    assert v["pv_s_l3"] == pytest.approx(v["pv_s_total"] / 3)
    assert v["pv_pf_l1"] == v["pv_pf_l2"] == v["pv_pf_l3"] == pytest.approx(0.999)
    assert v["pv_exp_energy_l1"] == pytest.approx(12_345.6 / 3)
    # frequency appears nowhere in the SmartLogger manual at all
    assert v["pv_freq"] == pytest.approx(50.0)
    # a PV plant has no import counter on this measurement point
    assert v["pv_imp_energy_total"] == 0.0
    assert v["pv_reactive_imp_energy_total"] == 0.0
    assert v["pv_reactive_exp_energy_total"] == 0.0


async def test_plant_covers_the_whole_canonical_model():
    """Every canonical point the DTSU maps consume exists under the pv_ prefix."""
    from nd45_dtsu666.config import STATIC_DEBUG_VALUE_KEYS

    _client, v = await _poll_plant()
    produced = {k.removeprefix("pv_") for k in v if k.startswith("pv_")}
    assert STATIC_DEBUG_VALUE_KEYS - produced == set()


async def test_plant_reads_one_block_on_logic_device_zero():
    client, _v = await _poll_plant()
    assert client.requests == [(40521, 57, 0)]


# --- power meter behind the SmartLogger --------------------------------------

METER_RAW = {
    32260: ("u32", 23_012),   # Phase A voltage, gain 100
    32262: ("u32", 23_015),
    32264: ("u32", 23_018),
    32266: ("u32", 39_850),   # A-B line voltage, gain 100
    32268: ("u32", 39_860),
    32270: ("u32", 39_870),
    32272: ("i32", 1_234),    # Phase A current, gain 10
    32274: ("i32", 2_345),
    32276: ("i32", 3_456),
    32278: ("i32", -60_000),  # Active power, gain 1000 kW -> W (exporting)
    32280: ("i32", 6_000),    # Reactive power, gain 1000 kVar -> var
    32284: ("i16", -950),     # Power factor, gain 1000
    32287: ("i32", 60_300),   # Apparent power, gain 1000 kVA -> VA
    32335: ("i32", -10_000),  # Phase A active power
    32337: ("i32", -20_000),
    32339: ("i32", -30_000),
    32349: ("i64", 300),      # Negative active electricity, gain 100
    32353: ("i64", 280),      # Negative reactive electricity, gain 100
    32357: ("i64", 700),      # Positive active electricity, gain 100
    32361: ("i64", 120),      # Positive reactive electricity, gain 100
}

METER_UNIT_ID = 11


async def _poll_meter(overrides=None, **kwargs):
    raw = {**METER_RAW, **(overrides or {})}
    client = FakeClient({a: _regs(val, dt) for a, (dt, val) in raw.items()})
    sources = [("huawei_meter", REGISTERS.huawei_meter_source, METER_UNIT_ID)]
    values = await huawei_poller.poll_once(client, sources, **kwargs)
    return client, values


async def test_meter_decodes_documented_registers():
    _client, v = await _poll_meter()
    assert v["mtr_u_l1"] == pytest.approx(230.12)
    assert v["mtr_u_l12"] == pytest.approx(398.5)
    assert v["mtr_i_l1"] == pytest.approx(123.4)
    assert v["mtr_p_total"] == pytest.approx(-60_000.0)
    assert v["mtr_q_total"] == pytest.approx(6_000.0)
    assert v["mtr_pf_total"] == pytest.approx(-0.95)
    assert v["mtr_s_total"] == pytest.approx(60_300.0)
    assert v["mtr_p_l1"] == pytest.approx(-10_000.0)
    assert v["mtr_p_l3"] == pytest.approx(-30_000.0)
    # I64 directional energy counters
    assert v["mtr_imp_energy_total"] == pytest.approx(7.0)
    assert v["mtr_exp_energy_total"] == pytest.approx(3.0)
    assert v["mtr_reactive_imp_energy_total"] == pytest.approx(1.2)
    assert v["mtr_reactive_exp_energy_total"] == pytest.approx(2.8)


async def test_meter_derives_per_phase_reactive_by_active_power_share():
    _client, v = await _poll_meter()
    # Q apportioned by each phase's share of |P|: 10/20/30 of 60 kW
    assert v["mtr_q_l1"] == pytest.approx(1_000.0)
    assert v["mtr_q_l2"] == pytest.approx(2_000.0)
    assert v["mtr_q_l3"] == pytest.approx(3_000.0)
    # S and PF then follow from the per-phase pair
    assert v["mtr_s_l1"] == pytest.approx(math.hypot(-10_000.0, 1_000.0))
    assert v["mtr_pf_l1"] == pytest.approx(-10_000.0 / math.hypot(-10_000.0, 1_000.0))
    assert v["mtr_imp_energy_l1"] == pytest.approx(7.0 / 3)
    assert v["mtr_exp_energy_l2"] == pytest.approx(3.0 / 3)
    assert v["mtr_freq"] == pytest.approx(50.0)


async def test_meter_covers_the_whole_canonical_model():
    from nd45_dtsu666.config import STATIC_DEBUG_VALUE_KEYS

    _client, v = await _poll_meter()
    produced = {k.removeprefix("mtr_") for k in v if k.startswith("mtr_")}
    assert STATIC_DEBUG_VALUE_KEYS - produced == set()


async def test_meter_reads_two_blocks_on_its_rs485_address():
    client, _v = await _poll_meter()
    assert client.requests == [(32260, 30, METER_UNIT_ID), (32335, 30, METER_UNIT_ID)]


async def test_ratio_split_falls_back_to_equal_when_weights_vanish():
    """At night every phase reports P = 0, so the proportions carry no signal."""
    _client, v = await _poll_meter(
        overrides={
            32335: ("i32", 0),
            32337: ("i32", 0),
            32339: ("i32", 0),
            32280: ("i32", 900),
        }
    )
    assert v["mtr_q_l1"] == v["mtr_q_l2"] == v["mtr_q_l3"] == pytest.approx(300.0)
    # purely reactive phase: S comes from Q alone, so PF is legitimately zero
    assert v["mtr_s_l1"] == pytest.approx(300.0)
    assert v["mtr_pf_l1"] == pytest.approx(0.0)


async def test_pf_is_unity_when_the_phase_is_fully_idle():
    """S == 0 would make pf_from_p_s divide by zero and poison the sample."""
    _client, v = await _poll_meter(
        overrides={
            32335: ("i32", 0),
            32337: ("i32", 0),
            32339: ("i32", 0),
            32280: ("i32", 0),
        }
    )
    assert v["mtr_s_l1"] == 0.0
    assert v["mtr_pf_l1"] == v["mtr_pf_l2"] == v["mtr_pf_l3"] == pytest.approx(1.0)


# --- both sources in one poll ------------------------------------------------

async def test_both_sources_poll_on_their_own_unit_ids():
    """The SmartLogger answers as device 0 for itself, as the RS485 address for a meter."""
    image = {**_plant_image()}
    image.update({a: _regs(val, dt) for a, (dt, val) in METER_RAW.items()})
    client = FakeClient(image)
    sources = [
        ("huawei_plant", REGISTERS.huawei_plant_source, 0),
        ("huawei_meter", REGISTERS.huawei_meter_source, METER_UNIT_ID),
    ]
    values = await huawei_poller.poll_once(client, sources)
    assert client.requests == [
        (40521, 57, 0),
        (32260, 30, METER_UNIT_ID),
        (32335, 30, METER_UNIT_ID),
    ]
    assert values["pv_p_total"] == pytest.approx(1_234_500.0)
    assert values["mtr_p_total"] == pytest.approx(-60_000.0)


async def test_huawei_poll_never_emits_unprefixed_canonical_names():
    """The DTSU energy aliases belong to the primary source and must stay untouched.

    compute_derived would fill active_energy_total / net_*_energy_total from the
    unprefixed keys, which a Huawei map does not have -- writing zeros over real
    ND45 meter energy.
    """
    image = {**_plant_image()}
    image.update({a: _regs(val, dt) for a, (dt, val) in METER_RAW.items()})
    client = FakeClient(image)
    sources = [
        ("huawei_plant", REGISTERS.huawei_plant_source, 0),
        ("huawei_meter", REGISTERS.huawei_meter_source, METER_UNIT_ID),
    ]
    values = await huawei_poller.poll_once(client, sources)
    assert not [k for k in values if not k.startswith(("pv_", "mtr_"))]


# --- invalid values, faults --------------------------------------------------

async def test_invalid_sentinel_is_zeroed_and_does_not_reject_the_sample():
    """Unlike ND45, a bad SmartLogger channel must not lose the whole poll.

    This source is telemetry that cannot gate the DTSU output, so a disconnected
    inverter's type-max sentinel zeroes one point and the rest still lands.
    """
    _client, v = await _poll_plant(
        overrides={40532: ("i16", int_sentinel("i16"))}
    )
    assert v["pv_pf_total"] == 0.0
    assert v["pv_p_total"] == pytest.approx(1_234_500.0)  # unaffected


async def test_invalid_sentinel_warns_once_per_episode(caplog):
    sources = [("huawei_plant", REGISTERS.huawei_plant_source, 0)]
    bad = FakeClient(_plant_image({40544: ("i32", int_sentinel("i32"))}))
    good = FakeClient(_plant_image())
    seen: set[str] = set()
    with caplog.at_level("WARNING", logger="nd45_dtsu666.huawei_poller"):
        await huawei_poller.poll_once(bad, sources, overrange_seen=seen)
        await huawei_poller.poll_once(bad, sources, overrange_seen=seen)
    assert seen == {"pv_q_total"}
    assert sum("pv_q_total invalid" in r.getMessage() for r in caplog.records) == 1
    with caplog.at_level("INFO", logger="nd45_dtsu666.huawei_poller"):
        await huawei_poller.poll_once(good, sources, overrange_seen=seen)
    assert seen == set()


async def test_derived_non_finite_is_scrubbed_before_reaching_the_store():
    """Nothing non-finite may reach the store; the output encoder would raise."""
    side = SourceSide.model_validate(
        {
            "read_groups": [{"base": 100, "count": 2}],
            "points": {"x_p_total": {"addr": 100, "dtype": "u32"}},
            "derive": [
                {"op": "pf_from_p_s", "targets": ["x_pf"], "from": ["x_p_total", "missing"]}
            ],
        }
    )
    client = FakeClient({100: _regs(5, "u32")})
    values = await huawei_poller.poll_once(client, [("x", side, 0)])
    assert values["x_pf"] == 0.0


async def test_address_offset_shifts_the_wire_address_only():
    """The bring-up knob for the vendor's 0- vs 1-based register numbering."""
    side = SourceSide.model_validate(
        {
            "address_offset": -1,
            "read_groups": [{"base": 40521, "count": 57}],
            "points": {"pv_p_total": {"addr": 40525, "dtype": "i32"}},
        }
    )
    # image is laid out one register lower, matching the shifted request
    client = FakeClient({40524: _regs(777, "i32")})
    values = await huawei_poller.poll_once(client, [("plant", side, 0)])
    assert client.requests == [(40520, 57, 0)]
    assert values["pv_p_total"] == pytest.approx(777.0)


async def test_read_error_raises_poll_error():
    class ErrorResponse:
        registers: list[int] = []

        def isError(self):
            return True

    class ErrorClient:
        async def read_holding_registers(self, address, count, slave=0):
            return ErrorResponse()

    sources = [("huawei_plant", REGISTERS.huawei_plant_source, 0)]
    with pytest.raises(huawei_poller.PollError, match="read error"):
        await huawei_poller.poll_once(ErrorClient(), sources)


async def test_short_read_raises_poll_error():
    class ShortClient:
        async def read_holding_registers(self, address, count, slave=0):
            return FakeResponse([0] * (count - 1))

    sources = [("huawei_plant", REGISTERS.huawei_plant_source, 0)]
    with pytest.raises(huawei_poller.PollError, match="short read"):
        await huawei_poller.poll_once(ShortClient(), sources)


# --- source selection -------------------------------------------------------

def _config(**huawei) -> AppConfig:
    return CONFIG.model_copy(
        update={"huawei": CONFIG.huawei.model_copy(update={"enabled": True, **huawei})}
    )


def test_select_sources_returns_both_when_both_unit_ids_are_set():
    config = _config(host="10.0.0.5", plant_unit_id=0, meter_unit_id=11)
    selected = huawei_poller.select_sources(config, REGISTERS)
    assert [(name, unit) for name, _side, unit in selected] == [
        ("huawei_plant", 0),
        ("huawei_meter", 11),
    ]


def test_select_sources_skips_a_source_with_no_unit_id():
    config = _config(host="10.0.0.5", plant_unit_id=0, meter_unit_id=None)
    selected = huawei_poller.select_sources(config, REGISTERS)
    assert [name for name, _side, _unit in selected] == ["huawei_plant"]


def test_select_sources_rejects_a_unit_id_with_no_register_section():
    config = _config(host="10.0.0.5", plant_unit_id=0, meter_unit_id=11)
    stripped = REGISTERS.model_copy(update={"huawei_meter_source": None})
    with pytest.raises(ValueError, match="no huawei_meter_source section"):
        huawei_poller.select_sources(config, stripped)


def test_select_sources_rejects_a_source_without_read_groups():
    """A Huawei source with no blocks would poll nothing and look like a dead device."""
    side = SourceSide.model_validate({"points": {"pv_p_total": {"addr": 40525}}})
    config = _config(host="10.0.0.5", plant_unit_id=0, meter_unit_id=None)
    broken = REGISTERS.model_copy(update={"huawei_plant_source": side})
    with pytest.raises(ValueError, match="must declare 'read_groups'"):
        huawei_poller.select_sources(config, broken)


def test_shipped_register_map_has_both_huawei_sources():
    assert isinstance(REGISTERS, RegisterMap)
    assert REGISTERS.huawei_plant_source is not None
    assert REGISTERS.huawei_meter_source is not None
