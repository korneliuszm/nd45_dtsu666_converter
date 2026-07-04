import asyncio

from nd45_dtsu666.app import build_on_update, build_pipeline, connect_with_retry
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


class _FlakyClient:
    def __init__(self, fail_times):
        self.calls = 0
        self._fail_times = fail_times

    async def connect(self):
        self.calls += 1
        return self.calls > self._fail_times


async def test_connect_with_retry_succeeds_after_failures():
    client = _FlakyClient(fail_times=2)
    ok = await connect_with_retry(client, asyncio.Event(), delay=0.001, max_delay=0.01)
    assert ok is True
    assert client.calls == 3  # failed twice, connected on the third attempt


async def test_connect_with_retry_returns_false_when_stopped():
    client = _FlakyClient(fail_times=999)
    stop = asyncio.Event()
    stop.set()  # asked to stop before ever connecting
    ok = await connect_with_retry(client, stop, delay=0.001, max_delay=0.01)
    assert ok is False
    assert client.calls == 0  # never even attempted


async def test_connect_with_retry_stops_between_attempts():
    client = _FlakyClient(fail_times=999)  # never connects
    stop = asyncio.Event()

    async def _stopper():
        await asyncio.sleep(0.01)
        stop.set()

    ok, _ = await asyncio.gather(
        connect_with_retry(client, stop, delay=0.005, max_delay=0.01), _stopper()
    )
    assert ok is False  # gives up cleanly once stop is set, never hangs
