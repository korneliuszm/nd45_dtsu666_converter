"""eTango source: multi-host averaging/summing, CDAB word order, strict faults.

No real socket is opened -- each simulated controller is a duck-typed fake
client serving a synthetic float32 register image, mirroring the Huawei
pattern in test_huawei_poller.py, but wired through `etango_poller.MultiHostClient`
since a real etango source spans several independent TCP connections.
"""

from __future__ import annotations

import math

import pytest

from nd45_dtsu666 import etango_poller
from nd45_dtsu666.codec import float_to_registers
from nd45_dtsu666.config import STATIC_DEBUG_VALUE_KEYS, RegisterMap, SourceSide, load_registers

REGISTERS = load_registers("config/registers.json")
SIDE = REGISTERS.etango_source


def _regs(value: float) -> list[int]:
    """Encode a float into the map's declared CDAB order (word_order=little,
    byte_order=big -- e2TANGO's documented B3,B4,B1,B2 register layout)."""
    return float_to_registers(value, word_order="little", byte_order="big")


class FakeResponse:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class FakeDeviceClient:
    """Serves a synthetic e2TANGO image and records every request."""

    def __init__(self, image: dict[int, float]):
        self.flat: dict[int, int] = {}
        for addr, value in image.items():
            for offset, reg in enumerate(_regs(value)):
                self.flat[addr + offset] = reg
        self.requests: list[tuple[int, int, int]] = []
        self.connected = True

    async def connect(self):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    async def read_input_registers(self, address, count, slave=0):
        self.requests.append((address, count, slave))
        return FakeResponse([self.flat.get(a, 0) for a in range(address, address + count)])


# One reading per point the etango_source map declares (addr -> SI value).
BASE_IMAGE = {
    0: 100.0,    # i_l1
    2: 101.0,    # i_l2
    4: 102.0,    # i_l3
    6: 400.1,    # u_l12
    8: 400.2,    # u_l23
    10: 400.3,   # u_l31
    12: 50.0,    # freq
    14: 30_000.0,   # p_total
    16: 5_000.0,    # q_total
    18: 30_400.0,   # s_total
    20: 0.95,    # pf_total
    24: 1_000.0,    # imp_energy_total (Ec+)
    26: 500.0,   # exp_energy_total (Ec-)
    28: 200.0,   # reactive_imp_energy_total (Eb+)
    30: 100.0,   # reactive_exp_energy_total (Eb-)
    48: 230.1,   # u_l1
    50: 230.2,   # u_l2
    52: 230.3,   # u_l3
    54: 10_000.0,   # p_l1
    56: 10_000.0,   # p_l2
    58: 10_000.0,   # p_l3
    60: 1_500.0,    # q_l1
    62: 1_700.0,    # q_l2
    64: 1_800.0,    # q_l3
}


def _image(overrides: dict[int, float] | None = None) -> dict[int, float]:
    return {**BASE_IMAGE, **(overrides or {})}


def _client_for(image: dict[int, float]) -> FakeDeviceClient:
    return FakeDeviceClient(image)


def _multi(images: list[dict[int, float]], aggregate: list[bool] | None = None) -> etango_poller.MultiHostClient:
    aggregate = aggregate or [True] * len(images)
    return etango_poller.MultiHostClient([
        etango_poller.DeviceLink(
            client=_client_for(image), unit_id=1, aggregate=agg, host=f"192.168.30.{5 + 2 * i}"
        )
        for i, (image, agg) in enumerate(zip(images, aggregate))
    ])


async def _poll(images, aggregate=None):
    client = _multi(images, aggregate)
    values = await etango_poller.poll_once(client, SIDE, 0)
    return client, values


async def test_decodes_documented_registers_with_the_cdab_word_order():
    """One device's own readings, unaggregated: values must decode as-is."""
    _client, v = await _poll([_image()])
    assert v["i_l1"] == pytest.approx(100.0)
    assert v["u_l12"] == pytest.approx(400.1)
    assert v["u_l1"] == pytest.approx(230.1)
    assert v["freq"] == pytest.approx(50.0)
    assert v["p_total"] == pytest.approx(30_000.0)
    assert v["pf_total"] == pytest.approx(0.95)


async def test_averages_voltage_current_frequency_pf_across_four_devices():
    images = [
        _image({0: 100.0, 6: 400.0, 12: 50.00, 20: 0.90}),
        _image({0: 102.0, 6: 402.0, 12: 50.02, 20: 0.92}),
        _image({0: 104.0, 6: 404.0, 12: 49.98, 20: 0.94}),
        _image({0: 106.0, 6: 406.0, 12: 50.00, 20: 0.96}),
    ]
    _client, v = await _poll(images)
    assert v["i_l1"] == pytest.approx((100.0 + 102.0 + 104.0 + 106.0) / 4)
    assert v["u_l12"] == pytest.approx((400.0 + 402.0 + 404.0 + 406.0) / 4)
    assert v["freq"] == pytest.approx((50.00 + 50.02 + 49.98 + 50.00) / 4)
    assert v["pf_total"] == pytest.approx((0.90 + 0.92 + 0.94 + 0.96) / 4)


async def test_sums_power_and_energy_across_four_devices():
    images = [
        _image({14: 10_000.0, 16: 1_000.0, 18: 10_050.0, 24: 100.0, 26: 50.0}),
        _image({14: 20_000.0, 16: 2_000.0, 18: 20_100.0, 24: 200.0, 26: 60.0}),
        _image({14: 30_000.0, 16: -1_000.0, 18: 30_017.0, 24: 300.0, 26: 70.0}),
        _image({14: -5_000.0, 16: 500.0, 18: 5_025.0, 24: 400.0, 26: 80.0}),
    ]
    _client, v = await _poll(images)
    assert v["p_total"] == pytest.approx(10_000.0 + 20_000.0 + 30_000.0 - 5_000.0)
    assert v["q_total"] == pytest.approx(1_000.0 + 2_000.0 - 1_000.0 + 500.0)
    assert v["imp_energy_total"] == pytest.approx(100.0 + 200.0 + 300.0 + 400.0)
    assert v["exp_energy_total"] == pytest.approx(50.0 + 60.0 + 70.0 + 80.0)
    # S is summed straight from each device's own S register, not recomputed
    # from the summed P/Q -- construct the two so they clearly disagree.
    summed_s_direct = 10_050.0 + 20_100.0 + 30_017.0 + 5_025.0
    hypot_of_summed_pq = math.hypot(
        10_000.0 + 20_000.0 + 30_000.0 - 5_000.0,
        1_000.0 + 2_000.0 - 1_000.0 + 500.0,
    )
    assert summed_s_direct != pytest.approx(hypot_of_summed_pq)
    assert v["s_total"] == pytest.approx(summed_s_direct)


async def test_per_phase_apparent_power_and_pf_are_derived_before_combination():
    """hypot/pf_from_p_s are non-linear: derive-then-sum != sum-then-derive."""
    images = [
        _image({54: 3_000.0, 60: 4_000.0}),  # device 1: p_l1=3000, q_l1=4000 -> s=5000
        _image({54: 6_000.0, 60: -8_000.0}),  # device 2: p_l1=6000, q_l1=-8000 -> s=10000
    ]
    _client, v = await _poll(images)
    derive_then_sum = math.hypot(3_000.0, 4_000.0) + math.hypot(6_000.0, -8_000.0)
    sum_then_derive = math.hypot(3_000.0 + 6_000.0, 4_000.0 + -8_000.0)
    assert derive_then_sum != pytest.approx(sum_then_derive)
    assert v["s_l1"] == pytest.approx(derive_then_sum)


async def test_any_device_read_error_fails_the_whole_sample():
    class ErrorResponse:
        registers: list[int] = []

        def isError(self):
            return True

    class ErrorClient:
        connected = True

        async def connect(self):
            return True

        def close(self):
            pass

        async def read_input_registers(self, address, count, slave=0):
            return ErrorResponse()

    good = FakeDeviceClient(_image())
    client = etango_poller.MultiHostClient([
        etango_poller.DeviceLink(client=good, unit_id=1, aggregate=True, host="192.168.30.5"),
        etango_poller.DeviceLink(client=ErrorClient(), unit_id=1, aggregate=True, host="192.168.30.7"),
    ])
    with pytest.raises(etango_poller.PollError, match="192.168.30.7"):
        await etango_poller.poll_once(client, SIDE, 0)


async def test_any_device_short_read_fails_the_whole_sample():
    class ShortClient:
        connected = True

        async def connect(self):
            return True

        def close(self):
            pass

        async def read_input_registers(self, address, count, slave=0):
            return FakeResponse([0] * (count - 1))

    client = etango_poller.MultiHostClient([
        etango_poller.DeviceLink(client=ShortClient(), unit_id=1, aggregate=True, host="192.168.30.5"),
    ])
    with pytest.raises(etango_poller.PollError, match="short read"):
        await etango_poller.poll_once(client, SIDE, 0)


async def test_a_non_aggregating_device_is_still_polled_but_excluded_from_combination():
    images = [
        _image({0: 100.0}),
        _image({0: 999.0}),  # excluded: must not shift the average
    ]
    _client, v = await _poll(images, aggregate=[True, False])
    assert v["i_l1"] == pytest.approx(100.0)


async def test_covers_every_canonical_name_the_output_maps_reference():
    _client, v = await _poll([_image(), _image(), _image(), _image()])
    assert STATIC_DEBUG_VALUE_KEYS - set(v) == set()


async def test_poll_fills_the_energy_aliases_the_dtsu_maps_read():
    _client, v = await _poll([_image({24: 100.0, 26: 60.0})])
    assert v["active_energy_total"] == pytest.approx(100.0 + 60.0)
    assert v["net_imp_energy_total"] == pytest.approx(100.0)
    assert v["net_exp_energy_total"] == pytest.approx(60.0)


async def test_poll_covers_every_canonical_name_the_output_maps_reference():
    _client, v = await _poll([_image(), _image()])
    required = {
        pt.from_
        for _name, target in REGISTERS.targets
        for pt in target.points.values()
    }
    assert required - set(v) == set()


def test_validate_source_coverage_rejects_a_source_without_read_groups():
    side = SourceSide.model_validate({"points": {"p_total": {"addr": 14}}})
    with pytest.raises(ValueError, match="must declare 'read_groups'"):
        etango_poller.validate_source_coverage(side)


def test_shipped_register_map_has_the_etango_source():
    assert isinstance(REGISTERS, RegisterMap)
    assert REGISTERS.etango_source is not None


def test_source_by_name_resolves_the_etango_section():
    assert REGISTERS.source_by_name("etango_source") is REGISTERS.etango_source
