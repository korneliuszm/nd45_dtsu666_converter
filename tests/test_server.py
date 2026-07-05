import asyncio

import pytest

from nd45_dtsu666.canonical import CanonicalStore, HealthGate
from nd45_dtsu666.codec import registers_to_float
from nd45_dtsu666.config import DtsuConf, load_registers
from nd45_dtsu666.dtsu_server import (
    RecordingSlaveContext,
    RtuActivity,
    _server_action,
    build_context,
    supervise_server,
    update_datastore,
)


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
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert registers_to_float(regs, target.word_order, target.byte_order) == pytest.approx(2300.0)


def test_missing_canonical_key_is_skipped():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    update_datastore(context, 1, {}, target)  # no values -> no crash
    regs = context[1].getValues(3, target.points["u_l1"].addr, count=2)
    assert regs == [0, 0]


def test_static_registers_absent_without_dtsu_cfg():
    target = load_registers("config/registers.json").dtsu_target
    context = build_context(target, slave_id=1)
    assert context[1].getValues(3, 0x0006, count=1) == [0]


def test_static_registers_seeded_from_dtsu_cfg():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(port="/dev/null", baudrate=9600, slave_id=7)
    context = build_context(target, slave_id=7, dtsu_cfg=cfg)
    slave = context[7]
    assert slave.getValues(3, 0x0000, count=1) == [100]  # REV.
    assert slave.getValues(3, 0x0003, count=1) == [0]  # net = 3P4W
    assert slave.getValues(3, 0x0006, count=1) == [10]  # IrAt = ratio 1.0
    assert slave.getValues(3, 0x0007, count=1) == [10]  # UrAt = ratio 1.0
    assert slave.getValues(3, 0x002D, count=1) == [3]  # bAud = 9600
    assert slave.getValues(3, 0x002E, count=1) == [7]  # Addr = slave_id


def test_static_registers_unknown_baudrate_defaults_to_9600_code():
    target = load_registers("config/registers.json").dtsu_target
    cfg = DtsuConf(port="/dev/null", baudrate=115200, slave_id=1)
    context = build_context(target, slave_id=1, dtsu_cfg=cfg)
    assert context[1].getValues(3, 0x002D, count=1) == [3]


class FakeServer:
    def __init__(self):
        self.running = False
        self.serve_calls = 0
        self.shutdown_calls = 0

    async def serve_forever(self):
        self.running = True
        self.serve_calls += 1
        while self.running:
            await asyncio.sleep(0.01)

    async def shutdown(self):
        self.running = False
        self.shutdown_calls += 1


async def test_supervisor_starts_when_fresh_and_stops_when_stale():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(port="/dev/null", slave_id=1)
    fake = FakeServer()
    stop = asyncio.Event()
    clock = {"t": 100.0}

    def now():
        return clock["t"]

    store.update({"p_total": 1.0}, ts=now())  # fresh
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=lambda: fake, now=now)
    )
    await asyncio.sleep(0.1)
    assert fake.running is True  # serving while fresh

    clock["t"] = 200.0  # data now stale (age 100s > 3s)
    await asyncio.sleep(0.1)
    assert fake.running is False  # stopped -> Sigenergy sees timeout

    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.serve_calls >= 1 and fake.shutdown_calls >= 1


class FailingServer:
    def __init__(self):
        self.serve_calls = 0

    async def serve_forever(self):
        self.serve_calls += 1
        raise RuntimeError("port busy")

    async def shutdown(self):
        pass


async def test_supervisor_retries_when_serve_fails():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(port="/dev/null", slave_id=1)
    created = []

    def factory():
        s = FailingServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: 100.0)
    )
    await asyncio.sleep(0.15)  # several check intervals while data stays fresh
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(created) >= 2  # recreated after failure => it retried


def test_rtu_activity_summary_tally_and_last_seen():
    a = RtuActivity()
    a.record(3, 8192, 64, ts=100.0)
    a.record(3, 8192, 64, ts=100.5)
    a.record(3, 4126, 24, ts=101.0)
    s = a.summary(now=102.0)
    assert s["total"] == 3
    assert s["last_seen_age"] == pytest.approx(1.0)  # 102.0 - 101.0
    blocks = dict(s["blocks"])
    assert blocks[(3, 8192, 64)] == 2
    assert blocks[(3, 4126, 24)] == 1
    assert s["recent"][-1] == (101.0, 3, 4126, 24)


def test_rtu_activity_summary_empty():
    s = RtuActivity().summary(now=5.0)
    assert s["total"] == 0
    assert s["last_seen_age"] is None
    assert s["blocks"] == []


def test_recording_context_records_reads_and_delegates():
    target = load_registers("config/registers.json").dtsu_target
    activity = RtuActivity()
    context = build_context(target, slave_id=1, activity=activity)
    update_datastore(context, 1, {"u_l1": 230.0}, target)
    addr = target.points["u_l1"].addr

    regs = context[1].getValues(3, addr, count=2)

    assert regs != [0, 0]  # real datastore values delegated through
    assert activity.total == 1  # the Sigenergy read was recorded
    s = activity.summary(now=activity.last_ts)
    assert ((3, addr, 2), 1) in s["blocks"]


def test_build_context_activity_optional():
    target = load_registers("config/registers.json").dtsu_target
    plain = build_context(target, slave_id=1)
    assert not isinstance(plain[1], RecordingSlaveContext)
    rec = build_context(target, slave_id=1, activity=RtuActivity())
    assert isinstance(rec[1], RecordingSlaveContext)


def test_server_action_decisions():
    # fresh + idle + never started -> start immediately
    assert _server_action(True, False, now=100.0, last_start=None, min_restart_interval=5.0) == "start"
    # fresh + idle but a restart would be too soon after the last start -> wait (anti-flap)
    assert _server_action(True, False, now=102.0, last_start=100.0, min_restart_interval=5.0) == "wait"
    # fresh + idle and enough time has passed -> start
    assert _server_action(True, False, now=106.0, last_start=100.0, min_restart_interval=5.0) == "start"
    # stale + running -> stop (fail-safe)
    assert _server_action(False, True, now=100.0, last_start=100.0, min_restart_interval=5.0) == "stop"
    # steady states -> no action
    assert _server_action(True, True, now=100.0, last_start=100.0, min_restart_interval=5.0) == "none"
    assert _server_action(False, False, now=100.0, last_start=100.0, min_restart_interval=5.0) == "none"


async def test_supervisor_throttles_restart_within_interval():
    store = CanonicalStore()
    gate = HealthGate(max_age=3.0)
    cfg = DtsuConf(port="/dev/null", slave_id=1)
    created = []

    class FailingServer:
        async def serve_forever(self):
            raise RuntimeError("port busy")

        async def shutdown(self):
            pass

    def factory():
        s = FailingServer()
        created.append(s)
        return s

    stop = asyncio.Event()
    store.update({"p_total": 1.0}, ts=100.0)
    task = asyncio.create_task(
        supervise_server(cfg, context=None, store=store, gate=gate,
                         check_interval=0.02, stop_event=stop,
                         server_factory=factory, now=lambda: 100.0,
                         min_restart_interval=5.0)
    )
    await asyncio.sleep(0.15)  # many check intervals, clock frozen at 100.0
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert len(created) == 1  # first server failed; retry throttled inside the interval
