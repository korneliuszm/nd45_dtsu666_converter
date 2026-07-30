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


def test_dashboard_can_label_static_debug_source():
    out = render_dashboard(
        _sample_canonical(),
        age=0.1,
        healthy=True,
        activity=RtuActivity(),
        slave_id=10,
        now=100.0,
        source_label="STATIC DEBUG",
    )

    assert "STATIC DEBUG" in out
    assert "ND45 (source)" not in out


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


# --- PV panel (Huawei SmartLogger) ------------------------------------------


def _pv_canonical():
    return {
        **_sample_canonical(),
        "pv_p_total": 1234500.0,
        "pv_q_total": -50000.0,
        "pv_pf_total": 0.999,
        "pv_exp_energy_total": 12345.6,
        "pv_e_daily": 456.7,
        "pv_dc_power": 1250000.0,
        "pv_i_l1": 100.0, "pv_i_l2": 101.0, "pv_i_l3": 102.0,
        "pv_u_l12": 400.1, "pv_u_l23": 400.2, "pv_u_l31": 400.3,
    }


def test_dashboard_has_no_pv_panel_without_a_secondary_source():
    text = render_dashboard(
        _sample_canonical(), 0.4, True, RtuActivity(), 10, now=100.0
    )
    assert "PV production" not in text


def test_dashboard_renders_the_pv_panel_when_pv_age_is_given():
    text = render_dashboard(
        _pv_canonical(), 0.4, True, RtuActivity(), 10, now=100.0, pv_age=2.5
    )
    assert "PV production (SmartLogger, telemetry)" in text
    assert "data age: 2.50s" in text
    assert "1234500.0" in text  # instantaneous PV production, the headline number
    assert "E_daily=456.7 kWh" in text
    assert "P_dc=1250000.0 W" in text


def test_pv_panel_is_labelled_telemetry_and_survives_a_healthy_bridge():
    """A stale PV panel next to a SERVING bridge is expected, not a fault."""
    text = render_dashboard(
        _pv_canonical(), 0.4, True, RtuActivity(), 10, now=100.0, pv_age=900.0
    )
    assert "state: SERVING" in text
    assert "telemetry" in text
    assert "data age: 900.00s" in text


def test_pv_panel_reports_never_polled_before_the_first_sample():
    text = render_dashboard(
        _sample_canonical(), 0.4, True, RtuActivity(), 10, now=100.0, pv_age=float("inf")
    )
    assert "(never polled)" in text
    assert "(no data yet)" in text
