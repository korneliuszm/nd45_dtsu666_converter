import asyncio

from nd45_dtsu666.app import build_on_update, build_pipeline
from nd45_dtsu666.canonical import CanonicalStore
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.dtsu_server import RecordingSlaveContext, RtuActivity, build_context


def test_on_update_writes_store_and_datastore():
    target = load_registers("config/registers.json").dtsu_target
    store = CanonicalStore()
    context = build_context(target, slave_id=1)
    on_update = build_on_update(store, context, 1, target)

    on_update({"u_l1": 230.0}, ts=5.0)

    values, ts = store.snapshot()
    assert values["u_l1"] == 230.0 and ts == 5.0
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == 2300.0


def test_build_pipeline_wires_components_and_threads_activity():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    activity = RtuActivity()

    pipe = build_pipeline(config, registers, stop, activity=activity, client=object())

    assert pipe.store is not None
    assert len(pipe.coros) == 2  # poller + supervisor
    # activity was threaded into a recording datastore context
    assert isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)

    for coro in pipe.coros:  # never awaited in this test; close to keep output pristine
        coro.close()


def test_build_pipeline_default_context_not_recording():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    assert not isinstance(pipe.context[config.dtsu.slave_id], RecordingSlaveContext)
    for coro in pipe.coros:
        coro.close()
