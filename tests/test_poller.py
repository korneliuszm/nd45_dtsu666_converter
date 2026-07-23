import asyncio
import struct

import pytest

from nd45_dtsu666.codec import float_to_registers
from nd45_dtsu666.config import SourcePoint, SourceSide, load_registers
from nd45_dtsu666.nd45_poller import (
    PollError,
    READ_GROUPS,
    compute_derived,
    extract_registers,
    poll_once,
    run_poller,
    validate_source_coverage,
)


def test_validate_source_coverage_passes_for_seed_config():
    validate_source_coverage(load_registers("config/registers.json").nd45_source)


def test_validate_source_coverage_rejects_uncovered_address():
    source = SourceSide(points={"bogus": SourcePoint(addr=200)})
    with pytest.raises(ValueError, match="not covered by READ_GROUPS"):
        validate_source_coverage(source)


def test_validate_source_coverage_rejects_uncovered_compose_address():
    source = SourceSide(
        points={"e": SourcePoint(compose=[900, 5000], factors=[1000, 1])}
    )
    with pytest.raises(ValueError, match="5000"):
        validate_source_coverage(source)


class FakeResponse:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class FakeClient:
    """Serves a synthetic ND45 register image for the three read groups."""

    def __init__(self, image: dict[int, list[int]]):
        # image maps absolute addr -> 2 registers (a float32)
        self.image = image

    async def read_holding_registers(self, address, count, slave=0):
        regs = []
        for a in range(address, address + count):
            pair = self.image.get(a)
            if pair is not None:
                regs.append(pair[0])
            elif a - 1 in self.image:
                regs.append(self.image[a - 1][1])
            else:
                regs.append(0)
        return FakeResponse(regs)


def _image_for(values: dict[int, float]) -> dict[int, list[int]]:
    return {addr: float_to_registers(v, "big", "big") for addr, v in values.items()}


def _raw_float_registers(value: float) -> list[int]:
    """Build a raw ND45 float, including specials rejected by the output codec."""
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def test_extract_registers_finds_pair():
    groups = [(50, [0x4366, 0x0000, 0x1111, 0x2222])]
    assert extract_registers(50, groups) == [0x4366, 0x0000]
    assert extract_registers(51, groups) == [0x0000, 0x1111]


def test_read_groups_cover_all_addresses():
    src = load_registers("config/registers.json").nd45_source
    covered = set()
    for base, count in READ_GROUPS:
        covered.update(range(base, base + count))
    for pt in src.points.values():
        addrs = pt.compose if pt.compose else [pt.addr]
        for a in addrs:
            assert a in covered and (a + 1) in covered, f"addr {a} not covered"


async def test_poll_once_decodes_points():
    src = load_registers("config/registers.json").nd45_source
    image = _image_for({
        50: 230.1, 128: 1500.0, 818: 50.02,
        900: 0.0, 902: 100.0,   # imp energy L1 -> 100 kWh
        904: 0.0, 906: 200.0,   # imp energy L2 -> 200 kWh
        908: 0.0, 910: 300.0,   # imp energy L3 -> 300 kWh
        912: 2.0, 914: 345.0,   # imp energy total -> 2345 kWh
        916: 0.0, 918: 3.0,     # exp energy L1 -> 3 kWh
        920: 0.0, 922: 4.0,     # exp energy L2 -> 4 kWh
        924: 0.0, 926: 5.0,     # exp energy L3 -> 5 kWh
        928: 0.0, 930: 12.0,    # exp energy total -> 12 kWh
        944: 1.0, 946: 10.0,    # imported inductive -> 1010 kvarh
        960: 0.0, 962: 20.0,    # exported inductive -> 20 kvarh
        976: 2.0, 978: 30.0,    # imported capacitive -> 2030 kvarh
        992: 0.0, 994: 40.0,    # exported capacitive -> 40 kvarh
    })
    client = FakeClient(image)
    values = await poll_once(client, src, slave=1)
    assert values["u_l1"] == pytest.approx(230.1, rel=1e-5)
    assert values["p_total"] == pytest.approx(1500.0, rel=1e-5)
    assert values["freq"] == pytest.approx(50.02, rel=1e-5)
    assert values["imp_energy_l1"] == pytest.approx(100.0, rel=1e-5)
    assert values["imp_energy_l2"] == pytest.approx(200.0, rel=1e-5)
    assert values["imp_energy_l3"] == pytest.approx(300.0, rel=1e-5)
    assert values["imp_energy_total"] == pytest.approx(2345.0, rel=1e-5)
    assert values["exp_energy_l1"] == pytest.approx(3.0, rel=1e-5)
    assert values["exp_energy_l2"] == pytest.approx(4.0, rel=1e-5)
    assert values["exp_energy_l3"] == pytest.approx(5.0, rel=1e-5)
    assert values["exp_energy_total"] == pytest.approx(12.0, rel=1e-5)
    assert values["reactive_energy_total"] == pytest.approx(3100.0, rel=1e-5)
    assert values["active_energy_total"] == pytest.approx(2357.0, rel=1e-5)
    assert values["net_imp_energy_total"] == pytest.approx(2345.0, rel=1e-5)
    assert values["net_exp_energy_total"] == pytest.approx(12.0, rel=1e-5)


def test_compute_derived_matches_physical_directional_aliases():
    values = {
        "imp_energy_total": 7.0078125,
        "exp_energy_total": 0.19921875,
    }

    compute_derived(values)

    assert values["active_energy_total"] == pytest.approx(7.20703125)
    assert values["net_imp_energy_total"] == pytest.approx(7.0078125)
    assert values["net_exp_energy_total"] == pytest.approx(0.19921875)


def test_extract_registers_raises_for_uncovered_address():
    groups = [(50, [0x1111, 0x2222, 0x3333, 0x4444])]
    with pytest.raises(KeyError):
        extract_registers(999, groups)


class _ShortReadClient:
    """Returns fewer registers than requested (truncated Modbus frame)."""

    async def read_holding_registers(self, address, count, slave=0):
        return FakeResponse([0] * (count - 1))


async def test_poll_once_raises_clear_error_on_short_read():
    src = load_registers("config/registers.json").nd45_source
    with pytest.raises(Exception, match="short read"):
        await poll_once(_ShortReadClient(), src, slave=1)


async def test_poll_once_substitutes_zero_only_for_invalid_power_factor():
    src = load_registers("config/registers.json").nd45_source
    image = {64: _raw_float_registers(float("nan"))}

    values = await poll_once(FakeClient(image), src, slave=1)

    assert values["pf_l1"] == 0.0


@pytest.mark.parametrize(
    ("address", "value", "point"),
    [
        (50, float("nan"), "u_l1"),
        (52, float("inf"), "i_l1"),
        (128, 3e20, "p_total"),
        (912, 3e20, "imp_energy_total"),
        (944, 3e20, "reactive_energy_total"),
    ],
)
async def test_poll_once_rejects_invalid_critical_value(address, value, point):
    src = load_registers("config/registers.json").nd45_source
    image = {address: _raw_float_registers(value)}

    with pytest.raises(PollError, match=point):
        await poll_once(FakeClient(image), src, slave=1)


async def test_poll_once_rejects_invalid_composite_parts_that_cancel(caplog):
    src = load_registers("config/registers.json").nd45_source
    image = {
        944: _raw_float_registers(3e20),
        960: _raw_float_registers(-3e20),
    }
    seen: set[str] = set()

    with caplog.at_level("WARNING", logger="nd45_dtsu666.nd45_poller"):
        for _ in range(2):
            with pytest.raises(PollError, match="reactive_energy_total"):
                await poll_once(
                    FakeClient(image), src, slave=1, overrange_seen=seen
                )

    warnings = [
        record
        for record in caplog.records
        if "reactive_energy_total" in record.getMessage()
    ]
    assert len(warnings) == 1


async def test_poll_once_reports_all_invalid_critical_points():
    src = load_registers("config/registers.json").nd45_source
    image = {
        50: _raw_float_registers(float("nan")),
        52: _raw_float_registers(float("inf")),
    }

    with pytest.raises(PollError, match=r"u_l1.*i_l1"):
        await poll_once(FakeClient(image), src, slave=1)


async def test_poll_once_invalid_warning_logged_once_per_episode(caplog):
    src = load_registers("config/registers.json").nd45_source
    invalid = {50: _raw_float_registers(3e20)}
    seen: set[str] = set()
    with caplog.at_level("WARNING", logger="nd45_dtsu666.nd45_poller"):
        for _ in range(2):
            with pytest.raises(PollError):
                await poll_once(
                    FakeClient(invalid), src, slave=1, overrange_seen=seen
                )
    warnings = [r for r in caplog.records if "u_l1" in r.getMessage()]
    assert len(warnings) == 1

    normal = _image_for({50: 230.0})
    values = await poll_once(FakeClient(normal), src, slave=1, overrange_seen=seen)
    assert values["u_l1"] == pytest.approx(230.0)
    assert "u_l1" not in seen


async def test_run_poller_does_not_publish_invalid_critical_sample():
    src = load_registers("config/registers.json").nd45_source
    client = FakeClient({50: _raw_float_registers(float("nan"))})
    stop = asyncio.Event()
    updates: list[tuple[dict[str, float], float]] = []
    errors: list[Exception] = []

    async def _stopper():
        await asyncio.sleep(0.04)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            run_poller(
                client,
                src,
                slave=1,
                interval=0.01,
                on_update=lambda values, ts: updates.append((values, ts)),
                on_error=errors.append,
                stop_event=stop,
            ),
            _stopper(),
        ),
        timeout=1.0,
    )

    assert updates == []
    assert errors and all(isinstance(exc, PollError) for exc in errors)


async def test_poll_once_reads_apparent_power_from_nd45():
    src = load_registers("config/registers.json").nd45_source
    image = _image_for({
        50: 230.0, 52: 2.0,  # U*I is deliberately different from ND45 S
        60: 471.0,
        84: 582.0,
        108: 693.0,
        132: 1801.0,  # independent ND45 total, not the phase sum (1746 VA)
    })
    values = await poll_once(FakeClient(image), src, slave=1)
    assert values["s_l1"] == pytest.approx(471.0)
    assert values["s_l2"] == pytest.approx(582.0)
    assert values["s_l3"] == pytest.approx(693.0)
    assert values["s_total"] == pytest.approx(1801.0)


async def test_poll_once_compose_applies_sign_scale_offset():
    from nd45_dtsu666.config import SourcePoint, SourceSide

    src = SourceSide(points={
        "energy": SourcePoint(compose=[900, 902], factors=[1000, 1], sign=-1),
    })
    image = _image_for({900: 2.0, 902: 345.0})  # composed: 2*1000 + 345 = 2345
    values = await poll_once(FakeClient(image), src, slave=1)
    assert values["energy"] == pytest.approx(-2345.0, rel=1e-5)


class _BrokenClient:
    """Every read fails, so run_poller's except-branch calls on_error every cycle."""

    async def read_holding_registers(self, address, count, slave=0):
        raise RuntimeError("nd45 unreachable")


async def test_run_poller_survives_on_error_callback_raising():
    src = load_registers("config/registers.json").nd45_source
    stop = asyncio.Event()

    def on_error(exc):
        raise ValueError("logging backend down") from exc

    async def _stopper():
        await asyncio.sleep(0.05)
        stop.set()

    # A bug in the error-handling callback itself must not kill the poller
    # loop -- "poller must never die" has to hold even when on_error raises.
    await asyncio.wait_for(
        asyncio.gather(
            run_poller(
                _BrokenClient(), src, slave=1, interval=0.01,
                on_update=lambda values, ts: None, on_error=on_error, stop_event=stop,
            ),
            _stopper(),
        ),
        timeout=1.0,
    )


async def test_run_poller_reports_on_update_exception_to_on_error():
    # Locks down current semantics: a bug in the SUCCESS callback (on_update)
    # is routed to on_error and the loop survives -- the poll itself worked,
    # only the datastore write broke, but the poller must keep cycling.
    src = load_registers("config/registers.json").nd45_source
    stop = asyncio.Event()
    errors: list[Exception] = []

    def on_update(values, ts):
        raise RuntimeError("datastore write failed")

    async def _stopper():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            run_poller(
                FakeClient({}), src, slave=1, interval=0.01,
                on_update=on_update, on_error=errors.append, stop_event=stop,
            ),
            _stopper(),
        ),
        timeout=1.0,
    )
    assert errors and isinstance(errors[0], RuntimeError)
