import asyncio
import math

import pytest
from pydantic import ValidationError

from nd45_dtsu666 import metrics
from nd45_dtsu666.canonical import CanonicalStore, HealthGate
from nd45_dtsu666.config import AppConfig, load_config, load_registers
from nd45_dtsu666.dtsu_server import RtuActivity, supervise_server
from nd45_dtsu666.metrics import (
    BridgeMetrics,
    MetricsSource,
    PollStats,
    RecoveryStats,
    ServerStatus,
    parse_request,
    render,
    route,
)
from nd45_dtsu666.watchdog import Heartbeat

P = metrics.PREFIX


def _bridge(config=None, spec=None, **overrides) -> BridgeMetrics:
    """A BridgeMetrics for the first configured bridge, with keyword overrides."""
    config = config or load_config("config/config.json")
    spec = spec or config.bridge_specs[0]
    kwargs = dict(
        name=spec.name,
        spec=spec,
        store=CanonicalStore(),
        poll_stats=PollStats(),
        server_status=ServerStatus(),
        recovery=RecoveryStats(),
        activity=RtuActivity(),
        heartbeat=Heartbeat(),
        client_ref=None,
    )
    kwargs.update(overrides)
    return BridgeMetrics(**kwargs)


def _source(**overrides) -> MetricsSource:
    config = overrides.pop("config", None) or load_config("config/config.json")
    registers = load_registers("config/registers.json")
    bridges = overrides.pop("bridges", None)
    if bridges is None:
        bridges = [_bridge(config=config)]
    kwargs = dict(
        config=config,
        bridges=bridges,
        targets=registers.targets,
        watchdog_heartbeat=Heartbeat(),
        mode="run",
        started_at=0.0,
    )
    kwargs.update(overrides)
    return MetricsSource(**kwargs)


class _ClientRef:
    """Stands in for a BridgeRuntime: BridgeMetrics reads `.client` off it."""

    def __init__(self, client):
        self.client = client


def _samples(text: str, name: str) -> list[tuple[str, str]]:
    """Return (labels, value) for every sample line of a metric family."""
    found = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if line == name or line.startswith(name + " ") or line.startswith(name + "{"):
            body = line[len(name):]
            labels, _, value = body.partition(" ")
            found.append((labels, value))
    return found


def _value(text: str, name: str, labels: str = "") -> str:
    matches = [v for lbl, v in _samples(text, name) if lbl == labels]
    assert len(matches) == 1, f"{name}{labels}: {matches}"
    return matches[0]


# --- formatting -------------------------------------------------------------


def test_fmt_handles_non_finite_values():
    assert metrics._fmt(math.inf) == "+Inf"
    assert metrics._fmt(-math.inf) == "-Inf"
    assert metrics._fmt(math.nan) == "NaN"
    assert metrics._fmt(0) == "0"
    assert metrics._fmt(-60000.0) == "-60000"
    assert metrics._fmt(0.25) == "0.25"


def test_label_values_are_escaped():
    assert metrics._esc('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_every_sample_line_has_a_type_declaration():
    text = render(_source(), loop_now=1.0, mono_now=1.0)
    declared = {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")}
    for line in text.splitlines():
        if line.startswith("#") or not line:
            continue
        name = line.split("{")[0].split(" ")[0]
        assert name in declared, name


# --- health metrics ---------------------------------------------------------


def test_empty_store_reports_infinite_age_and_not_fresh():
    text = render(_source(), loop_now=100.0, mono_now=100.0)
    assert _value(text, f"{P}_nd45_data_age_seconds") == "+Inf"
    assert _value(text, f"{P}_nd45_data_fresh") == "0"
    assert _value(text, f"{P}_nd45_last_successful_poll_age_seconds") == "+Inf"
    assert _value(text, f"{P}_dtsu_last_request_age_seconds") == "+Inf"
    assert _value(text, f"{P}_dtsu_server_up") == "0"


def test_fresh_store_reports_age_and_freshness():
    source = _source()
    source.primary.store.update({"p_total": -60000.0}, ts=98.0)
    text = render(source, loop_now=100.0, mono_now=100.0)
    assert _value(text, f"{P}_nd45_data_age_seconds") == "2"
    assert _value(text, f"{P}_nd45_data_fresh") == "1"  # max_data_age_s = 3.0
    text = render(source, loop_now=104.0, mono_now=104.0)
    assert _value(text, f"{P}_nd45_data_fresh") == "0"


def test_data_age_and_request_age_use_separate_clocks():
    """Data age is on the loop clock, request age on time.monotonic()."""
    source = _source()
    source.primary.store.update({"p_total": 1.0}, ts=1000.0)  # loop clock
    source.primary.activity.record(3, 0x2000, 2, ts=5.0)  # monotonic clock
    text = render(source, loop_now=1001.0, mono_now=7.0)
    assert _value(text, f"{P}_nd45_data_age_seconds") == "1"
    assert _value(text, f"{P}_dtsu_last_request_age_seconds") == "2"


def test_nd45_connected_omitted_without_a_client_and_reported_with_one():
    text = render(_source(), loop_now=1.0, mono_now=1.0)
    assert f"{P}_nd45_connected" not in text

    class _Client:
        connected = True

    source = _source(bridges=[_bridge(client_ref=_ClientRef(_Client()))])
    text = render(source, loop_now=1.0, mono_now=1.0)
    assert _value(text, f"{P}_nd45_connected") == "1"


def test_build_info_carries_version_mode_and_bridge_count():
    text = render(_source(mode="static"), loop_now=1.0, mono_now=1.0)
    line = next(ln for ln in text.splitlines() if ln.startswith(f"{P}_build_info"))
    assert 'mode="static"' in line
    assert 'bridges="1"' in line
    assert line.endswith(" 1")


def test_heartbeat_age_exported():
    """The alias family now reports event-loop liveness, not poller progress."""
    source = _source()
    source.watchdog_heartbeat.touch(50.0)
    text = render(source, loop_now=1.0, mono_now=52.5)
    assert _value(text, f"{P}_watchdog_heartbeat_age_seconds") == "2.5"


# --- poll statistics --------------------------------------------------------


def test_poll_stats_counts_and_clears_consecutive_failures():
    stats = PollStats()
    stats.record_ok(1.0)
    stats.record_error(ValueError("boom"), 2.0)
    stats.record_error(RuntimeError("boom"), 3.0)
    assert (stats.polls_ok, stats.polls_failed, stats.consecutive_failures) == (1, 2, 2)
    assert stats.last_error_type == "RuntimeError"
    stats.record_ok(4.0)
    assert stats.consecutive_failures == 0
    assert stats.last_ok_ts == 4.0


def test_poll_stats_rendered_with_error_type_but_no_message():
    stats = PollStats()
    stats.record_ok(10.0)
    stats.record_error(ValueError("secret 192.168.1.10 detail"), 12.0)
    source = _source(bridges=[_bridge(poll_stats=stats)])
    text = render(source, loop_now=1.0, mono_now=14.0)
    assert _value(text, f"{P}_nd45_polls_total", '{result="ok"}') == "1"
    assert _value(text, f"{P}_nd45_polls_total", '{result="error"}') == "1"
    assert _value(text, f"{P}_nd45_consecutive_poll_failures") == "1"
    assert _value(text, f"{P}_nd45_last_poll_error_age_seconds") == "2"
    assert _value(text, f"{P}_nd45_last_poll_error_info", '{type="ValueError"}') == "1"
    assert "secret" not in text


def test_error_info_absent_before_any_failure():
    text = render(_source(), loop_now=1.0, mono_now=1.0)
    assert f"{P}_nd45_last_poll_error_info" not in text


async def test_build_pipeline_counts_polls_through_the_existing_callbacks(monkeypatch):
    """The counters ride on build_pipeline's on_update/on_error; the poller is untouched."""
    from nd45_dtsu666.app import build_pipeline

    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    stop = asyncio.Event()

    class _Client:
        def __init__(self):
            self.calls = 0

        async def read_holding_registers(self, base, count, slave=1):
            self.calls += 1
            if self.calls > 3:
                stop.set()
                raise OSError("link down")

            class _RR:
                registers = [0] * count

                def isError(self):
                    return False

            return _RR()

    pipe = build_pipeline(config, registers, stop, client=_Client(), only="nd45")
    config.bridges[0].source.poll_interval_s = 0.0
    poller, supervisor = pipe.coros
    supervisor.close()
    await asyncio.wait_for(poller, timeout=5)

    stats = pipe.metrics.primary.poll_stats
    assert stats.polls_ok >= 1
    assert stats.polls_failed >= 1
    assert stats.last_error_type == "OSError"


# --- DTSU output / Sigenergy activity ---------------------------------------


def test_request_counters_and_blocks_rendered():
    source = _source()
    source.primary.activity.record(3, 8192, 10, ts=1.0)
    source.primary.activity.record(3, 8192, 10, ts=1.5)
    source.primary.activity.record(4, 6144, 2, ts=2.0)
    text = render(source, loop_now=1.0, mono_now=3.0)
    assert _value(text, f"{P}_dtsu_requests_total") == "3"
    assert _value(text, f"{P}_dtsu_last_request_age_seconds") == "1"
    assert _value(
        text, f"{P}_dtsu_block_reads_total", '{fc="3",addr="8192",count="10"}'
    ) == "2"
    assert _value(
        text, f"{P}_dtsu_block_reads_total", '{fc="4",addr="6144",count="2"}'
    ) == "1"


async def test_server_status_follows_the_supervisor():
    config = load_config("config/config.json")
    store = CanonicalStore()
    gate = HealthGate(3.0)
    status = ServerStatus()
    stop = asyncio.Event()
    clock = {"t": 100.0}

    class _FakeServer:
        async def serve_forever(self):
            await asyncio.Event().wait()

        async def shutdown(self):
            pass

    store.update({}, ts=100.0)  # fresh -> supervisor should start the server

    async def _drive():
        await asyncio.sleep(0)
        while not status.running:
            await asyncio.sleep(0)
        assert status.starts == 1
        assert status.last_start_ts == 100.0
        clock["t"] = 200.0  # data now 100s old -> stale -> fail-safe stop
        while status.running:
            await asyncio.sleep(0)
        assert status.stops == 1
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            supervise_server(
                config.bridge_specs[0].dtsu, object(), store, gate, 0.0, stop,
                server_factory=_FakeServer, now=lambda: clock["t"], status=status,
            ),
            _drive(),
        ),
        timeout=5,
    )
    assert status.running is False


# --- measurement and register gauges ----------------------------------------


def test_canonical_values_exported_by_point():
    source = _source()
    source.primary.store.update({"p_total": -60000.0, "freq": 50.0}, ts=1.0)
    text = render(source, loop_now=1.0, mono_now=1.0)
    assert _value(text, f"{P}_canonical_value", '{point="p_total"}') == "-60000"
    assert _value(text, f"{P}_canonical_value", '{point="freq"}') == "50"


def test_dtsu_register_value_matches_the_encoded_register():
    """The gauge must go through the same encode path the datastore uses."""
    source = _source()
    registers = load_registers("config/registers.json")
    point = registers.dtsu_sigen_ext_target.points["u_l1"]
    source.primary.store.update({"u_l1": 240.0}, ts=1.0)
    text = render(source, loop_now=1.0, mono_now=1.0)
    labels = (
        f'{{map="dtsu_sigen_ext_target",point="u_l1",'
        f'fc="{registers.dtsu_sigen_ext_target.function_code}",addr="{point.addr}"}}'
    )
    assert float(_value(text, f"{P}_dtsu_register_value", labels)) == 240.0 * point.scale


def test_register_read_age_is_infinite_until_read_then_finite():
    source = _source()
    registers = load_registers("config/registers.json")
    side = registers.dtsu_sigen_ext_target
    point = side.points["u_l1"]
    labels = (
        f'{{map="dtsu_sigen_ext_target",point="u_l1",'
        f'fc="{side.function_code}",addr="{point.addr}"}}'
    )
    text = render(source, loop_now=1.0, mono_now=10.0)
    assert _value(text, f"{P}_dtsu_register_read_age_seconds", labels) == "+Inf"

    source.primary.activity.record(side.function_code, point.addr, 2, ts=8.0)
    text = render(source, loop_now=1.0, mono_now=10.0)
    assert _value(text, f"{P}_dtsu_register_read_age_seconds", labels) == "2"


def test_include_registers_false_drops_the_value_gauges():
    config = load_config("config/config.json")
    config.prometheus.include_registers = False
    source = _source(config=config)
    source.primary.store.update({"p_total": 1.0}, ts=1.0)
    text = render(source, loop_now=1.0, mono_now=1.0)
    assert f"{P}_canonical_value" not in text
    assert f"{P}_dtsu_register_value" not in text
    assert f"{P}_dtsu_requests_total" in text  # health metrics stay


def test_unencodable_value_skips_only_that_register():
    """An out-of-float32-range SI value must not break the whole scrape."""
    source = _source()
    source.primary.store.update({"p_total": 1e300, "freq": 50.0}, ts=1.0)
    text = render(source, loop_now=1.0, mono_now=1.0)
    assert _value(text, f"{P}_canonical_value", '{point="freq"}') == "50"
    assert not [lbl for lbl, _ in _samples(text, f"{P}_dtsu_register_value")
                if 'point="p_total"' in lbl]


# --- HTTP layer (pure functions; no sockets, per CLAUDE.md) -----------------


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"GET /metrics HTTP/1.1\r\n", "/metrics"),
        (b"GET /metrics?x=1 HTTP/1.1\r\n", "/metrics"),
        (b"get / HTTP/1.0\r\n", "/"),
        (b"POST /metrics HTTP/1.1\r\n", None),
        (b"garbage\r\n", None),
        (b"", None),
    ],
)
def test_parse_request(head, expected):
    assert parse_request(head) == expected


def test_route_metrics_returns_exposition():
    status, content_type, body = route("/metrics", _source(), 1.0, 1.0)
    assert status == 200
    assert content_type == metrics.CONTENT_TYPE
    assert body.startswith(f"# HELP {P}_build_info")
    assert body.endswith("\n")


def test_route_healthz_reflects_freshness():
    source = _source()
    assert route("/healthz", source, 1.0, 1.0)[0] == 503
    source.primary.store.update({}, ts=1.0)
    status, _, body = route("/healthz", source, 1.0, 1.0)
    assert status == 200
    assert body.startswith("ok")


def test_route_index_and_unknown_path():
    assert route("/", _source(), 1.0, 1.0)[0] == 200
    assert route("/nope", _source(), 1.0, 1.0)[0] == 404
    assert route(None, _source(), 1.0, 1.0)[0] == 404


def test_response_headers_close_the_connection():
    raw = metrics._response(200, "text/plain", "hi\n")
    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Length: 3\r\n" in raw
    assert b"Connection: close\r\n" in raw
    assert raw.endswith(b"\r\n\r\nhi\n")


# --- start/stop helper ------------------------------------------------------


async def test_start_returns_none_when_disabled_or_sourceless():
    config = load_config("config/config.json")
    config.prometheus.enabled = False
    assert metrics.start(config, _source(), asyncio.Event()) is None
    config.prometheus.enabled = True
    assert metrics.start(config, None, asyncio.Event()) is None
    await metrics.stop(None)


async def test_serve_metrics_retries_a_failed_bind_instead_of_raising(monkeypatch):
    attempts = []

    async def _boom(*args, **kwargs):
        attempts.append(args)
        raise OSError("address in use")

    monkeypatch.setattr(asyncio, "start_server", _boom)
    config = load_config("config/config.json")
    stop = asyncio.Event()
    task = asyncio.create_task(
        metrics.serve_metrics(config.prometheus, _source(), stop, rebind_delay=0.01)
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(attempts) >= 2  # kept retrying, never raised


# --- configuration ----------------------------------------------------------


def test_prometheus_defaults_and_optional_section():
    config = AppConfig.model_validate(
        {
            "nd45": {"host": "1.2.3.4"},
            "dtsu": {"transport": "rtu", "rtu": {"port": "/dev/ttyAMA2"}},
        }
    )
    assert config.prometheus.enabled is True
    assert config.prometheus.host == "0.0.0.0"
    assert config.prometheus.port == 9090
    assert config.prometheus.include_registers is True


def test_prometheus_port_range_validated():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "nd45": {"host": "1.2.3.4"},
                "dtsu": {"transport": "rtu", "rtu": {"port": "/dev/ttyAMA2"}},
                "prometheus": {"port": 70000},
            }
        )


def test_prometheus_port_colliding_with_dtsu_tcp_rejected():
    with pytest.raises(ValidationError, match="collides"):
        AppConfig.model_validate(
            {
                "nd45": {"host": "1.2.3.4"},
                "dtsu": {"transport": "tcp", "tcp": {"host": "0.0.0.0", "port": 502}},
                "prometheus": {"port": 502},
            }
        )


def test_prometheus_port_collision_ignored_for_rtu_transport():
    config = AppConfig.model_validate(
        {
            "nd45": {"host": "1.2.3.4"},
            "dtsu": {
                "transport": "rtu",
                "rtu": {"port": "/dev/ttyAMA2"},
                "tcp": {"host": "0.0.0.0", "port": 502},
            },
            "prometheus": {"port": 502},
        }
    )
    assert config.prometheus.port == 502


def test_shipped_config_files_load():
    import glob

    for path in sorted(glob.glob("config/config*.json")):
        load_config(path)


# --- multiple independent bridges ------------------------------------------


SMARTLOGGER_BRIDGE = {
    "name": "smartlogger",
    "enabled": True,
    "source": {
        "type": "huawei", "host": "10.0.0.5", "register_map": "huawei_plant_source",
    },
    "dtsu": {
        "transport": "rtu", "slave_id": 10,
        "rtu": {"port": "/dev/ttyAMA3"},
    },
    "safety": {"max_data_age_s": 30.0},
}


def _two_bridge_config() -> AppConfig:
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"] = [raw["bridges"][0], SMARTLOGGER_BRIDGE]
    return AppConfig.model_validate(raw)


def _two_bridge_source(**overrides) -> MetricsSource:
    config = _two_bridge_config()
    bridges = [_bridge(config=config, spec=spec) for spec in config.bridge_specs]
    return _source(config=config, bridges=bridges, **overrides)


def test_single_bridge_emits_no_second_bridge_series():
    """A one-bridge scrape must stay as it was before multi-bridge support."""
    text = render(_source(), loop_now=10.0, mono_now=10.0)
    assert _samples(text, f"{P}_bridge_data_age_seconds") == [('{bridge="nd45"}', "+Inf")]
    assert 'bridge="smartlogger"' not in text


def test_each_bridge_gets_its_own_labelled_series():
    source = _two_bridge_source()
    nd45, smart = source.bridges
    nd45.store.update({"p_total": -60000.0}, ts=9.5)
    smart.store.update({"p_total": 1234500.0}, ts=8.0)

    text = render(source, loop_now=10.0, mono_now=10.0)

    assert _samples(text, f"{P}_bridge_data_age_seconds") == [
        ('{bridge="nd45"}', "0.5"),
        ('{bridge="smartlogger"}', "2"),
    ]


def test_each_bridge_is_judged_against_its_own_freshness_threshold():
    """20s stale is fine for a 5s-polled SmartLogger (30s) but not for the ND45 (3s)."""
    source = _two_bridge_source()
    nd45, smart = source.bridges
    nd45.store.update({"p_total": 1.0}, ts=0.0)
    smart.store.update({"p_total": 1.0}, ts=0.0)

    text = render(source, loop_now=20.0, mono_now=20.0)

    assert _samples(text, f"{P}_bridge_data_fresh") == [
        ('{bridge="nd45"}', "0"),
        ('{bridge="smartlogger"}', "1"),
    ]
    assert _samples(text, f"{P}_bridge_max_data_age_seconds") == [
        ('{bridge="nd45"}', "3"),
        ('{bridge="smartlogger"}', "30"),
    ]


def test_bridge_info_names_the_source_and_the_output_it_serves():
    text = render(_two_bridge_source(), loop_now=1.0, mono_now=1.0)
    labels = [labels for labels, _v in _samples(text, f"{P}_bridge_info")]
    assert 'bridge="nd45"' in labels[0]
    assert 'source_type="nd45"' in labels[0]
    assert 'output="/dev/ttyAMA2"' in labels[0]
    assert 'bridge="smartlogger"' in labels[1]
    assert 'source_type="huawei"' in labels[1]
    assert 'register_map="huawei_plant_source"' in labels[1]
    assert 'output="/dev/ttyAMA3"' in labels[1]


def test_output_server_state_is_reported_per_bridge():
    """The whole point of two bridges: one can be silenced while the other serves."""
    source = _two_bridge_source()
    nd45, smart = source.bridges
    nd45.server_status.started(5.0)

    text = render(source, loop_now=10.0, mono_now=10.0)

    assert _samples(text, f"{P}_bridge_dtsu_server_up") == [
        ('{bridge="nd45"}', "1"),
        ('{bridge="smartlogger"}', "0"),
    ]


def test_poller_restarts_counter_is_per_bridge():
    source = _two_bridge_source()
    source.bridges[1].recovery.record(7.0)
    source.bridges[1].recovery.record(9.0)

    text = render(source, loop_now=10.0, mono_now=10.0)

    assert _samples(text, f"{P}_bridge_poller_restarts_total") == [
        ('{bridge="nd45"}', "0"),
        ('{bridge="smartlogger"}', "2"),
    ]
    assert _samples(text, f"{P}_bridge_last_poller_restart_age_seconds") == [
        ('{bridge="nd45"}', "+Inf"),
        ('{bridge="smartlogger"}', "1"),
    ]


def test_canonical_values_are_labelled_by_bridge():
    """Both bridges use plain canonical names, so the label is what separates them."""
    source = _two_bridge_source()
    source.bridges[0].store.update({"p_total": -60000.0}, ts=9.0)
    source.bridges[1].store.update({"p_total": 1234500.0}, ts=9.0)

    text = render(source, loop_now=10.0, mono_now=10.0)

    assert _samples(text, f"{P}_bridge_canonical_value") == [
        ('{bridge="nd45",point="p_total"}', "-60000"),
        ('{bridge="smartlogger",point="p_total"}', "1234500"),
    ]


def test_primary_aliases_keep_the_original_unlabelled_families():
    """Existing dashboards scrape these; adding a bridge must not break them."""
    source = _two_bridge_source()
    source.bridges[0].store.update({"p_total": -60000.0}, ts=9.5)

    text = render(source, loop_now=10.0, mono_now=10.0)

    assert _samples(text, f"{P}_nd45_data_age_seconds") == [("", "0.5")]
    assert _samples(text, f"{P}_nd45_data_fresh") == [("", "1")]
    assert _samples(text, f"{P}_dtsu_server_up") == [("", "0")]
    assert _samples(text, f"{P}_canonical_value") == [('{point="p_total"}', "-60000")]
    # ...and they describe the FIRST bridge only, never a mix
    assert _samples(text, f"{P}_nd45_max_data_age_seconds") == [("", "3")]


def test_healthz_is_ok_only_when_every_bridge_is_fresh():
    source = _two_bridge_source()
    nd45, smart = source.bridges
    nd45.store.update({"p_total": 1.0}, ts=9.9)
    smart.store.update({"p_total": 1.0}, ts=9.9)

    status, _ct, body = route("/healthz", source, loop_now=10.0, mono_now=10.0)
    assert status == 200
    assert "nd45=" in body and "smartlogger=" in body

    # one stale bridge is enough to fail the check, and it is named
    status, _ct, body = route("/healthz", source, loop_now=45.0, mono_now=45.0)
    assert status == 503
    assert "stale: nd45, smartlogger" in body

    # ...and a bridge within its own looser threshold is not blamed
    nd45.store.update({"p_total": 1.0}, ts=44.9)
    status, _ct, body = route("/healthz", source, loop_now=45.0, mono_now=45.0)
    assert status == 503
    assert "stale: smartlogger" in body


def test_watchdog_heartbeat_is_exported():
    """Tracks poll-loop progress; in-process recovery gets first crack at a stall."""
    source = _source()
    source.watchdog_heartbeat.touch(9.0)
    text = render(source, loop_now=10.0, mono_now=10.0)
    assert _samples(text, f"{P}_watchdog_heartbeat_age_seconds") == [("", "1")]


def test_bridge_heartbeat_and_stall_timeout_are_exported():
    source = _two_bridge_source()
    source.bridges[1].heartbeat.touch(4.0)
    text = render(source, loop_now=10.0, mono_now=10.0)
    assert _samples(text, f"{P}_bridge_heartbeat_age_seconds") == [
        ('{bridge="nd45"}', "+Inf"),
        ('{bridge="smartlogger"}', "6"),
    ]
    assert _samples(text, f"{P}_bridge_stall_timeout_seconds") == [
        ('{bridge="nd45"}', "30"),
        ('{bridge="smartlogger"}', "60"),
    ]


def test_every_bridge_family_declares_a_type():
    source = _two_bridge_source()
    for bridge in source.bridges:
        bridge.poll_stats.record_error(RuntimeError("x"), 1.0)
        bridge.recovery.record(1.0)
    text = render(source, loop_now=10.0, mono_now=10.0)
    declared = {line.split()[2] for line in text.splitlines() if line.startswith("# TYPE ")}
    emitted = {
        line.split("{")[0].split(" ")[0]
        for line in text.splitlines()
        if line and not line.startswith("#")
    }
    assert emitted <= declared


def test_bus_traffic_is_exported_per_bridge_and_slave_id():
    """Lets an alert fire on "frames arrive but none are ours" -- a wrong address."""
    from nd45_dtsu666.dtsu_server import WireActivity, rtu_crc

    def frame(slave):
        body = bytes([slave, 3]) + (8198).to_bytes(2, "big") + (2).to_bytes(2, "big")
        return body + rtu_crc(body).to_bytes(2, "little")

    wire = WireActivity()
    wire.record(frame(3), ts=1.0)
    config = _two_bridge_config()
    bridges = [_bridge(config=config, spec=spec) for spec in config.bridge_specs]
    bridges[1].wire = wire
    text = render(_source(config=config, bridges=bridges), loop_now=10.0, mono_now=10.0)

    assert _value(text, f"{P}_bridge_dtsu_bus_bytes_total", '{bridge="smartlogger"}') == "8"
    assert _value(
        text, f"{P}_bridge_dtsu_bus_frames_total", '{bridge="smartlogger",slave_id="3"}'
    ) == "1"
    # the bridge without a wire tracker emits no bus series at all
    assert f'{P}_bridge_dtsu_bus_bytes_total{{bridge="nd45"}}' not in text
