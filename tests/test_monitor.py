import asyncio

import nd45_dtsu666.monitor as monitor_mod
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.dtsu_server import RtuActivity
from nd45_dtsu666.monitor import render_dashboard


def _sample_canonical():
    return {
        "u_l1": 230.1, "u_l2": 231.0, "u_l3": 229.4,
        "i_l1": 5.02, "i_l2": 4.98, "i_l3": 5.10,
        "p_l1": 1153.0, "p_l2": 1140.0, "p_l3": 1160.0,
        "q_l1": 180.0, "q_l2": 175.0, "q_l3": 182.0,
        "pf_l1": 0.988, "pf_l2": 0.987, "pf_l3": 0.986,
        "p_total": 3453.0, "q_total": 537.0, "pf_total": 0.987, "freq": 50.01,
        "imp_energy_total": 1234.5, "exp_energy_total": 67.8,
    }


def test_dashboard_shows_nd45_values_and_import_direction():
    activity = RtuActivity()
    activity.record(3, 8192, 64, ts=100.0)
    out = render_dashboard(
        _sample_canonical(), age=0.31, healthy=True, activity=activity, slave_id=1, now=100.2
    )
    assert "230.1" in out            # phase L1 voltage
    assert "3453.0" in out           # total active power
    assert "50.01" in out            # frequency
    assert "IMPORT" in out           # p_total > 0
    assert "SERVING" in out          # healthy
    assert "1234.5" in out and "67.8" in out  # import/export energy
    assert "@8192 x64" in out        # RTU block that was read
    assert "requests: 1" in out


def test_dashboard_export_direction_and_silent_when_stale():
    values = _sample_canonical()
    values["p_total"] = -1500.0
    out = render_dashboard(
        values, age=99.0, healthy=False, activity=RtuActivity(), slave_id=1, now=100.0
    )
    assert "EXPORT" in out
    assert "SILENT" in out or "FAIL-SAFE" in out


def test_dashboard_handles_missing_values_without_crashing():
    out = render_dashboard({}, age=1.0, healthy=True, activity=RtuActivity(), slave_id=1, now=1.0)
    assert "requests: 0" in out
    assert "L1" in out  # phase rows still render with placeholders


async def test_run_monitor_returns_cleanly_when_never_connected(monkeypatch):
    # Locks the not-connected early-return wiring: coros closed (no
    # "coroutine never awaited" warnings), client closed, prompt return --
    # a regression here breaks commissioning startup in a hard-to-spot way.
    async def _never_connect(client, stop_event, *args, **kwargs):
        return False

    monkeypatch.setattr(monitor_mod, "connect_with_retry", _never_connect)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    await asyncio.wait_for(
        monitor_mod.run_monitor(config, registers, asyncio.Event()), timeout=1.0
    )
