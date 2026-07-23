import asyncio

import pytest

from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.dtsu_server import RtuActivity
from nd45_dtsu666.static_debug import build_static_pipeline, expand_static_values


def test_expand_static_values_preserves_configured_and_zero_fills_missing():
    registers = load_registers("config/registers.json")

    values = expand_static_values(registers, {"u_l1": 9000.0, "p_total": -12345.0})

    required = {
        point.from_
        for target in (registers.dtsu_target, registers.dtsu_sigen_ext_target)
        for point in target.points.values()
    }
    assert set(values) == required
    assert values["u_l1"] == 9000.0
    assert values["p_total"] == -12345.0
    assert values["i_l1"] == 0.0
    assert values["imp_energy_total"] == 0.0


async def test_static_feeder_writes_both_maps_and_keeps_store_fresh():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    pipe = build_static_pipeline(config, registers, stop, RtuActivity())

    assert not hasattr(pipe, "client")
    feeder = asyncio.create_task(pipe.coros[0])
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(feeder, timeout=1.0)
    pipe.coros[1].close()

    values, timestamp = pipe.store.snapshot()
    assert values["u_l1"] == 9000.0
    assert timestamp is not None
    assert pipe.store.age(asyncio.get_running_loop().time()) < 1.0

    classic = registers.dtsu_target.points["u_l1"]
    classic_regs = pipe.context[config.dtsu.slave_id].getValues(3, classic.addr, count=2)
    assert registers_to_float(classic_regs, "big", "big") == pytest.approx(90000.0)
    sigen = registers.dtsu_sigen_ext_target.points["u_l1"]
    sigen_regs = pipe.context[config.dtsu.slave_id].getValues(4, sigen.addr, count=2)
    assert registers_to_float(sigen_regs, "big", "big") == pytest.approx(9000.0)


def test_static_pipeline_contains_no_nd45_client_or_poller():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_static_pipeline(config, registers, asyncio.Event(), RtuActivity())

    assert not hasattr(pipe, "client")
    assert len(pipe.coros) == 2  # static feeder + output server supervisor
    for coro in pipe.coros:
        coro.close()
