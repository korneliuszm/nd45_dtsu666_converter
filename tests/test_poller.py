import asyncio

import pytest

from nd45_dtsu666.codec import float_to_registers
from nd45_dtsu666.config import load_registers
from nd45_dtsu666.nd45_poller import READ_GROUPS, extract_registers, poll_once, run_poller


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
    assert values["net_imp_energy_total"] == pytest.approx(2333.0, rel=1e-5)
    assert values["net_exp_energy_total"] == pytest.approx(0.0, abs=1e-9)


def test_extract_registers_raises_for_uncovered_address():
    groups = [(50, [0x1111, 0x2222, 0x3333, 0x4444])]
    with pytest.raises(KeyError):
        extract_registers(999, groups)


async def test_poll_once_substitutes_zero_for_nan():
    # NaN fails every >= comparison, so a plain abs(si) >= OVERRANGE guard
    # would let it through to Sigenergy -- it must be masked to 0.0 like
    # any other invalid reading (e.g. PF undefined at ~0 A phase current).
    src = load_registers("config/registers.json").nd45_source
    image = _image_for({50: float("nan")})
    values = await poll_once(FakeClient(image), src, slave=1)
    assert values["u_l1"] == 0.0


async def test_poll_once_substitutes_zero_for_inf():
    src = load_registers("config/registers.json").nd45_source
    image = _image_for({50: float("inf")})
    values = await poll_once(FakeClient(image), src, slave=1)
    assert values["u_l1"] == 0.0


async def test_poll_once_overrange_warning_logged_once_per_episode(caplog):
    src = load_registers("config/registers.json").nd45_source
    over = _image_for({50: 3e20})  # ND45 writes 2e20 when out of range
    seen: set[str] = set()
    with caplog.at_level("WARNING", logger="nd45_dtsu666.nd45_poller"):
        await poll_once(FakeClient(over), src, slave=1, overrange_seen=seen)
        await poll_once(FakeClient(over), src, slave=1, overrange_seen=seen)
    warnings = [r for r in caplog.records if "u_l1" in r.getMessage()]
    assert len(warnings) == 1  # sustained overrange must not flood the journal

    caplog.clear()
    normal = _image_for({50: 230.0})
    with caplog.at_level("INFO", logger="nd45_dtsu666.nd45_poller"):
        values = await poll_once(FakeClient(normal), src, slave=1, overrange_seen=seen)
    assert values["u_l1"] == pytest.approx(230.0, rel=1e-5)
    assert "u_l1" not in seen  # recovered -> a future episode logs again


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
