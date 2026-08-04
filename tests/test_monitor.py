"""The commissioning dashboard: link state, decoded values, Sigenergy activity.

`render_bridge_monitor` is pure -- it takes a BridgeSnapshot -- so these tests need
no running loop or live bridge. `run_monitor` itself is covered only for clean
startup/shutdown, since it opens real transports.
"""

import asyncio
import math

import pytest

import nd45_dtsu666.monitor as monitor_mod
from nd45_dtsu666.config import AppConfig, load_config, load_registers
from nd45_dtsu666.dtsu_server import RtuActivity
from nd45_dtsu666.monitor import (
    BridgeSnapshot,
    output_desc,
    render_bridge_monitor,
    select_bridge_by_source,
    snapshot_bridge,
    source_desc,
    source_kind,
)

SMARTLOGGER_BRIDGE = {
    "name": "smartlogger",
    "enabled": True,
    "source": {
        "type": "huawei",
        "host": "192.168.22.120",
        "register_map": "huawei_plant_source",
    },
    "dtsu": {
        "transport": "rtu",
        "slave_id": 10,
        "identity": {"ir_at": 200},
        "rtu": {"port": "/dev/ttyAMA3"},
    },
    "safety": {"max_data_age_s": 30.0},
}


def _two_bridge_config() -> AppConfig:
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"] = [raw["bridges"][0], SMARTLOGGER_BRIDGE]
    return AppConfig.model_validate(raw)


def _grid_values():
    return {
        "u_l1": 240.0, "u_l2": 240.0, "u_l3": 240.0,
        "i_l1": 83.3, "i_l2": 83.3, "i_l3": 83.3,
        "p_l1": -20000.0, "p_l2": -20000.0, "p_l3": -20000.0, "p_total": -60000.0,
        "q_l1": 2000.0, "q_l2": 2000.0, "q_l3": 2000.0, "q_total": 6000.0,
        "pf_l1": -0.95, "pf_l2": -0.95, "pf_l3": -0.95, "pf_total": -0.95,
        "freq": 50.02, "imp_energy_total": 7.0, "exp_energy_total": 3.0,
    }


def _activity(total=0, rate=0.0, last=None, blocks=(), recent=()):
    return {
        "total": total, "rate": rate, "last_seen_age": last,
        "blocks": list(blocks), "recent": list(recent),
    }


def _healthy_nd45(**overrides) -> BridgeSnapshot:
    kwargs = dict(
        name="nd45", source_kind="ND45", source_desc="192.168.22.109:502 unit 1",
        output_desc="/dev/ttyAMA2 @9600 8N1", slave_id=10,
        canonical=_grid_values(), data_age=0.28, max_data_age=3.0, poll_interval=0.3,
        source_connected=True, polls_ok=12043, last_ok_age=0.28,
        server_up=True, server_starts=1,
        activity=_activity(8421, 2.3, 0.4, [((3, 8192, 40), 4210)], [(1, 3, 8192, 40)]),
    )
    kwargs.update(overrides)
    return BridgeSnapshot(**kwargs)


def _dead_smartlogger(**overrides) -> BridgeSnapshot:
    kwargs = dict(
        name="smartlogger", source_kind="Huawei SmartLogger",
        source_desc="192.168.22.120:502 unit 0",
        output_desc="/dev/ttyAMA3 @9600 8N1", slave_id=10,
        max_data_age=30.0, poll_interval=5.0, source_connected=False,
        polls_failed=17, consecutive_failures=17, last_error_age=4.9,
        last_error_type="ConnectionException", server_up=False,
        activity=_activity(),
    )
    kwargs.update(overrides)
    return BridgeSnapshot(**kwargs)


# --- upstream link state (the question bring-up asks first) ------------------


def test_link_shows_connected_and_the_source_endpoint():
    text = render_bridge_monitor(_healthy_nd45())
    assert "link: CONNECTED" in text
    assert "192.168.22.109:502 unit 1" in text


def test_link_shows_down_for_an_unreachable_smartlogger():
    """The headline reason this panel exists: is the SmartLogger link up at all."""
    text = render_bridge_monitor(_dead_smartlogger())
    assert "link: DOWN" in text
    assert "Huawei SmartLogger" in text
    assert "192.168.22.120:502 unit 0" in text


def test_link_is_unknown_when_the_client_cannot_report_it():
    text = render_bridge_monitor(_healthy_nd45(source_connected=None))
    assert "link: UNKNOWN" in text


def test_poll_counters_and_last_error_are_shown():
    text = render_bridge_monitor(_dead_smartlogger())
    assert "polls: 0 ok / 17 err" in text
    assert "consecutive fails: 17" in text
    assert "last ok: never" in text
    assert "last error: ConnectionException (4.9s ago)" in text


def test_last_error_line_is_omitted_when_there_was_none():
    text = render_bridge_monitor(_healthy_nd45(last_error_type=None))
    assert "last error:" not in text


def test_poller_restarts_are_visible():
    """A rising count is the signal that stall recovery is looping."""
    text = render_bridge_monitor(
        _healthy_nd45(poller_restarts=3, last_error_type="TimeoutError")
    )
    assert "poller restarts: 3" in text


def test_never_polled_reads_as_words_not_infinity():
    text = render_bridge_monitor(_dead_smartlogger())
    assert "data age: never polled" in text
    assert "inf" not in text


# --- Sigenergy activity on this bridge's own port ----------------------------


def test_output_panel_names_the_port_and_shows_reads():
    """'Is Sigen reading on the second port' -- answered per bridge."""
    text = render_bridge_monitor(_healthy_nd45(output_desc="/dev/ttyAMA3 @9600 8N1"))
    assert "on /dev/ttyAMA3 @9600 8N1" in text
    assert "Sigenergy reads: 8421" in text
    assert "rate: 2.3/s" in text
    assert "last seen: 0.4s ago" in text
    assert "FC03 @8192 x40 (4210)" in text


def test_output_panel_says_so_when_sigenergy_has_not_polled():
    text = render_bridge_monitor(_dead_smartlogger())
    assert "Sigenergy has not polled this port" in text
    assert "last seen: never" in text


def test_server_state_reflects_the_fail_safe():
    assert "server: UP" in render_bridge_monitor(_healthy_nd45())
    silent = render_bridge_monitor(_dead_smartlogger())
    assert "server: SILENT (fail-safe)" in silent
    assert "state: FAIL-SAFE SILENT" in silent


def test_activity_absent_is_reported_rather_than_faked():
    text = render_bridge_monitor(_healthy_nd45(activity=None))
    assert "Sigenergy reads: (not recorded)" in text


# --- decoded source values ---------------------------------------------------


def test_per_phase_and_total_values_are_rendered():
    text = render_bridge_monitor(_healthy_nd45())
    assert "240.0" in text and "83.30" in text
    assert "-60000.0" in text and "6000.0" in text
    assert "f=50.02 Hz" in text
    assert "EXPORT (P<0)" in text
    assert "E_imp=7.0" in text and "E_exp=3.0" in text


def test_import_direction_is_labelled():
    values = {**_grid_values(), "p_total": 1234500.0}
    assert "IMPORT (P>0)" in render_bridge_monitor(_healthy_nd45(canonical=values))


def test_missing_values_render_as_dashes_not_crashes():
    text = render_bridge_monitor(_dead_smartlogger(canonical={}))
    assert "Direction: ?" in text
    assert " - " in text


def test_smartlogger_extras_are_shown_when_present():
    """e_daily / dc_power have no DTSU register but are useful during bring-up."""
    values = {**_grid_values(), "e_daily": 456.7, "dc_power": 1250000.0}
    text = render_bridge_monitor(_healthy_nd45(canonical=values))
    assert "E_daily=456.7" in text
    assert "P_dc=1250000.0" in text


def test_extras_line_is_omitted_for_a_source_without_them():
    assert "E_daily" not in render_bridge_monitor(_healthy_nd45())


# --- the served-register table is gone ---------------------------------------


def test_monitor_no_longer_dumps_the_dtsu_register_table():
    """It pushed the link and activity lines off screen; rtudebug traces registers."""
    text = render_bridge_monitor(_healthy_nd45())
    assert "DTSU666 output registers" not in text
    assert "reg raw" not in text
    assert not hasattr(monitor_mod, "render_registers_table")


# --- header ------------------------------------------------------------------


def test_header_keeps_the_state_separated_from_a_long_bridge_name():
    text = render_bridge_monitor(_healthy_nd45(name="a-very-long-bridge-name-here"))
    header = text.splitlines()[0]
    assert "DTSU666   state: SERVING" in header


# --- snapshot building from a live bridge ------------------------------------


class _Bridge:
    """Minimal stand-in for app.BridgeRuntime."""

    def __init__(self, spec, values, ts, connected=True):
        from nd45_dtsu666.canonical import CanonicalStore
        from nd45_dtsu666.metrics import PollStats, RecoveryStats, ServerStatus

        self.name = spec.name
        self.spec = spec
        self.store = CanonicalStore()
        self.store.update(values, ts)
        self.poll_stats = PollStats()
        self.poll_stats.record_ok(5.0)
        self.server_status = ServerStatus()
        self.server_status.started(5.0)
        self.recovery = RecoveryStats()
        self.activity = RtuActivity()
        self.client = type("C", (), {"connected": connected})()


def test_snapshot_bridge_reads_both_clocks_correctly():
    """Data age on the loop clock, activity ages on the monotonic clock."""
    spec = _two_bridge_config().bridge_specs[1]
    bridge = _Bridge(spec, {"p_total": 1234500.0}, ts=90.0)

    snap = snapshot_bridge(bridge, loop_now=92.5, mono_now=8.0)

    assert snap.name == "smartlogger"
    assert snap.data_age == pytest.approx(2.5)  # loop clock
    assert snap.last_ok_age == pytest.approx(3.0)  # monotonic clock
    assert snap.source_connected is True
    assert snap.max_data_age == 30.0
    assert snap.poll_interval == 5.0
    assert snap.server_up is True
    assert snap.canonical["p_total"] == pytest.approx(1234500.0)


def test_snapshot_bridge_reports_an_unreported_link_as_none():
    spec = _two_bridge_config().bridge_specs[0]
    bridge = _Bridge(spec, {}, ts=1.0)
    bridge.client = object()  # a fake client with no `connected` attribute
    assert snapshot_bridge(bridge, 1.0, 1.0).source_connected is None


def test_snapshot_of_a_never_polled_bridge_is_infinitely_old():
    from nd45_dtsu666.canonical import CanonicalStore

    spec = _two_bridge_config().bridge_specs[1]
    bridge = _Bridge(spec, {}, ts=1.0)
    bridge.store = CanonicalStore()
    snap = snapshot_bridge(bridge, 100.0, 100.0)
    assert snap.data_age == math.inf
    assert snap.healthy is False


# --- descriptors and bridge selection ----------------------------------------


def test_descriptors_name_the_source_and_the_output():
    nd45, smart = _two_bridge_config().bridge_specs
    assert source_kind(nd45) == "ND45"
    assert source_kind(smart) == "Huawei SmartLogger"
    assert source_desc(smart) == "192.168.22.120:502 unit 0"
    assert output_desc(nd45) == "/dev/ttyAMA2 @9600 8N1"
    assert output_desc(smart) == "/dev/ttyAMA3 @9600 8N1"


def test_output_desc_covers_the_tcp_transport():
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"][0]["dtsu"]["transport"] = "tcp"
    raw["bridges"][0]["dtsu"]["tcp"] = {"host": "0.0.0.0", "port": 502}
    spec = AppConfig.model_validate(raw).bridge_specs[0]
    assert output_desc(spec) == "tcp 0.0.0.0:502"


def test_select_bridge_by_source_finds_each_kind():
    """monitor_nd45 / monitor_hsm resolve by source type, not by a hardcoded name."""
    config = _two_bridge_config()
    assert select_bridge_by_source(config, "nd45") == "nd45"
    assert select_bridge_by_source(config, "huawei") == "smartlogger"


def test_select_bridge_by_source_finds_a_renamed_bridge():
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"] = [raw["bridges"][0], {**SMARTLOGGER_BRIDGE, "name": "pv-farm"}]
    config = AppConfig.model_validate(raw)
    assert select_bridge_by_source(config, "huawei") == "pv-farm"


def test_select_bridge_by_source_explains_what_is_configured():
    config = load_config("config/config.json")  # smartlogger bridge disabled
    with pytest.raises(SystemExit, match="no enabled bridge with source type 'huawei'"):
        select_bridge_by_source(config, "huawei")


# --- run_monitor lifecycle ---------------------------------------------------


async def test_run_monitor_returns_cleanly_when_stopped_immediately():
    # Locks the shutdown wiring: every coroutine finishes (no "coroutine never
    # awaited" warnings), every bridge's client is closed, prompt return.
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(monitor_mod.run_monitor(config, registers, stop), timeout=5.0)


async def test_run_monitor_rejects_an_unknown_bridge_name():
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(KeyError, match="no bridge named 'nope'"):
        await monitor_mod.run_monitor(config, registers, stop, bridge_name="nope")


# --- bus traffic (the wrong-address case) ------------------------------------
#
# pymodbus drops a frame addressed to a slave id the server does not hold, so
# "Sigenergy reads: 0" alone cannot tell a silent bus from a wrong address.


def _wire(bytes_seen=0, last=None, slave_ids=()):
    return {"bytes_seen": bytes_seen, "last_seen_age": last, "slave_ids": list(slave_ids)}


def test_a_silent_bus_says_nothing_is_transmitting():
    text = render_bridge_monitor(_dead_smartlogger(wire=_wire()))
    assert "bus traffic:  none -- nothing is transmitting on this port" in text


def test_traffic_for_a_foreign_slave_id_is_called_out():
    """The signature of a Sigenergy configured for the wrong Modbus address."""
    snap = _dead_smartlogger(
        server_up=True, activity=_activity(total=0), wire=_wire(96, 0.2, [(3, 12)])
    )
    text = render_bridge_monitor(snap)
    assert "frames for: slave 3 x12" in text
    assert "WRONG ADDRESS (this bridge answers slave 10)" in text


def test_traffic_for_the_right_slave_id_is_not_flagged():
    snap = _healthy_nd45(wire=_wire(4096, 0.1, [(10, 512)]))
    text = render_bridge_monitor(snap)
    assert "frames for: slave 10 x512" in text
    assert "WRONG ADDRESS" not in text


def test_bytes_without_a_decodable_frame_point_at_framing():
    """Something is on the wire but nothing frames -- baudrate/parity/wiring."""
    snap = _dead_smartlogger(server_up=True, activity=_activity(), wire=_wire(240, 1.0))
    assert "no valid Modbus frame decoded" in render_bridge_monitor(snap)


def test_a_tcp_output_has_no_bus_line():
    text = render_bridge_monitor(_healthy_nd45(wire=None))
    assert "bus traffic" not in text


# --- source spread (a swinging meter vs a swinging bridge) -------------------


def _spread(**stat):
    base = {"n": 30, "min": -3000.0, "max": 2600.0, "last": -100.0, "sign_changes": 0}
    base.update(stat)
    return {"p_total": base}


def test_spread_line_reports_the_window_and_the_swing():
    text = render_bridge_monitor(_healthy_nd45(spread=_spread()))
    assert "P spread (last 30 polls): -3000 .. 2600 W" in text
    assert "peak-peak 5600" in text


def test_spread_line_calls_out_sign_flips_only_when_there_are_any():
    flipping = render_bridge_monitor(_healthy_nd45(spread=_spread(sign_changes=17)))
    assert "sign flips: 17" in flipping
    steady = render_bridge_monitor(_healthy_nd45(spread=_spread(sign_changes=0)))
    assert "sign flips" not in steady


def test_a_bridge_that_has_never_polled_shows_no_spread_line():
    assert "P spread" not in render_bridge_monitor(_dead_smartlogger(spread={}))


async def test_snapshot_carries_the_spread_the_poller_recorded():
    """The rolling history is fed by on_update, so it reaches the screen."""
    from nd45_dtsu666.app import SPREAD_POINTS, build_pipeline
    from nd45_dtsu666.canonical import SampleStats

    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    pipe = build_pipeline(config, registers, asyncio.Event(), client=object())
    bridge = pipe.bridges[0]
    assert isinstance(bridge.spread, SampleStats)
    for value in (-500.0, 400.0, -300.0):
        bridge.spread.record({name: value for name in SPREAD_POINTS})

    snap = monitor_mod.snapshot_bridge(bridge, loop_now=1.0, mono_now=1.0)
    assert snap.spread["p_total"]["sign_changes"] == 2
    assert snap.spread["p_total"]["min"] == -500.0
    for coro in pipe.coros:
        coro.close()
