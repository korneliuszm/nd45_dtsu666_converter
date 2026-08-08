"""MQTT measurement publisher: config, topics, payload, publish loop."""

from __future__ import annotations

import asyncio
import json
import math

import pytest
from pydantic import ValidationError

from nd45_dtsu666 import mqtt_publisher
from nd45_dtsu666.canonical import CanonicalStore, HealthGate
from nd45_dtsu666.config import (
    DERIVED_CANONICAL_KEYS,
    MQTT_POINT_KEYS,
    STATIC_DEBUG_VALUE_KEYS,
    MqttConf,
    load_config,
    load_mqtt_config,
)
from nd45_dtsu666.mqtt_publisher import (
    STATUS_FRESH,
    STATUS_STALE,
    BridgePublish,
    MqttSource,
    build_payload,
    build_source,
)

MQTT_PATH = "config/mqtt.json"


def _conf(**overrides) -> MqttConf:
    kwargs = dict(enabled=True, publish_interval_s=0.01, points=["p_total"])
    kwargs.update(overrides)
    return MqttConf(**kwargs)


def _bridge(**overrides) -> BridgePublish:
    kwargs = dict(
        name="nd45",
        store=CanonicalStore(),
        gate=HealthGate(3.0),
        measurement_topic="nd45-dtsu666/nd45/measurements",
        status_topic="nd45-dtsu666/nd45/status",
    )
    kwargs.update(overrides)
    return BridgePublish(**kwargs)


def _source(**overrides) -> MqttSource:
    conf = overrides.pop("conf", None) or _conf()
    kwargs = dict(
        conf=conf,
        client_id="nd45-dtsu666-nd45",
        availability_topic="nd45-dtsu666/nd45/availability",
        bridges=[_bridge()],
    )
    kwargs.update(overrides)
    return MqttSource(**kwargs)


def _fed(values: dict[str, float], *, at: float = 100.0) -> CanonicalStore:
    """A store holding `values`, stamped with a loop-clock time."""
    store = CanonicalStore()
    store.update(values, at)
    return store


def _live_bridge(**overrides) -> BridgePublish:
    """A bridge whose sample is fresh *against the running loop's clock*.

    The store is stamped with loop.time(), so a fixture stamped at 0.0 reads as
    hours old inside a real loop. Call this from async tests only.
    """
    now = asyncio.get_running_loop().time()
    overrides.setdefault("store", _fed({"p_total": 1.0}, at=now))
    return _bridge(**overrides)


class _Boom(Exception):
    """A broker failure that is deliberately not an aiomqtt type.

    publish_loop must survive whatever paho leaks, not just MqttError, and the
    pure tests must not need aiomqtt installed to prove it.
    """


class _FakeClient:
    """Duck-typed aiomqtt client recording every publish. Never opens a socket."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.entered = 0

    async def __aenter__(self) -> "_FakeClient":
        self.entered += 1
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def publish(self, topic, payload, qos=0, retain=False, timeout=None) -> None:
        self.published.append((topic, payload, qos, retain))

    def topics(self) -> list[str]:
        return [topic for topic, _p, _q, _r in self.published]

    def payloads_for(self, topic: str) -> list[str]:
        return [p for t, p, _q, _r in self.published if t == topic]


def _stop_after(client: _FakeClient, stop_event: asyncio.Event, publishes: int):
    """Wrap publish so the loop stops itself after N messages -- no wall-clock waits."""
    inner = client.publish

    async def _publish(topic, payload, qos=0, retain=False, timeout=None):
        await inner(topic, payload, qos=qos, retain=retain, timeout=timeout)
        if len(client.published) >= publishes:
            stop_event.set()

    client.publish = _publish
    return client


# --- configuration ---------------------------------------------------------


def test_mqtt_defaults_are_off_and_publish_p_total_every_200ms():
    conf = MqttConf()
    assert conf.enabled is False
    assert conf.points == ["p_total"]
    assert conf.publish_interval_s == 0.2
    assert conf.qos == 0
    assert conf.retain is False


def test_shipped_mqtt_config_loads():
    conf = load_mqtt_config(MQTT_PATH)
    assert conf is not None
    assert conf.points == ["p_total"]
    assert conf.publish_interval_s == 0.2


def test_shipped_mqtt_config_is_disabled_so_merging_it_changes_nothing():
    assert load_mqtt_config(MQTT_PATH).enabled is False


def test_a_missing_mqtt_file_is_not_an_error():
    # Every existing deployment and every config_debug_*.json run predates this
    # file; upgrading the checkout must not change their behaviour.
    assert load_mqtt_config("config/definitely-not-here.json") is None


def test_a_malformed_mqtt_file_still_raises(tmp_path):
    path = tmp_path / "mqtt.json"
    path.write_text(json.dumps({"qos": 9}), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_mqtt_config(str(path))


def test_unknown_point_is_rejected():
    with pytest.raises(ValidationError, match="unknown mqtt point"):
        MqttConf.model_validate({"points": ["p_totl"]})


def test_duplicate_points_are_rejected():
    with pytest.raises(ValidationError, match="duplicate mqtt point"):
        MqttConf.model_validate({"points": ["p_total", "p_total"]})


def test_empty_point_list_is_rejected():
    with pytest.raises(ValidationError, match="publishing nothing"):
        MqttConf.model_validate({"points": []})


def test_huawei_only_points_are_accepted():
    # dc_power/e_daily exist only on huawei_plant_source. Allowed so a SmartLogger
    # deployment needs no code change; a bridge that lacks one omits it and warns.
    assert MqttConf.model_validate({"points": ["dc_power", "e_daily"]}).points == [
        "dc_power",
        "e_daily",
    ]


def test_mqtt_point_keys_cover_the_static_debug_and_derived_sets():
    assert STATIC_DEBUG_VALUE_KEYS <= MQTT_POINT_KEYS
    assert DERIVED_CANONICAL_KEYS <= MQTT_POINT_KEYS


def test_every_shipped_point_is_producible_by_every_source():
    # The shipped list must work whichever bridge a process runs.
    shipped = set(load_mqtt_config(MQTT_PATH).points)
    assert shipped <= (STATIC_DEBUG_VALUE_KEYS | DERIVED_CANONICAL_KEYS)


@pytest.mark.parametrize("prefix", ["a/+/b", "a/#", "/leading", "trailing/"])
def test_topic_prefix_rejects_wildcards_and_edge_slashes(prefix):
    with pytest.raises(ValidationError, match="topic_prefix"):
        MqttConf.model_validate({"topic_prefix": prefix})


def test_publish_interval_must_be_positive():
    with pytest.raises(ValidationError):
        MqttConf.model_validate({"publish_interval_s": 0})


def test_reconnect_backoff_max_must_not_be_below_the_initial_delay():
    with pytest.raises(ValidationError, match="reconnect_delay_max_s"):
        MqttConf.model_validate(
            {"broker": {"reconnect_delay_s": 10.0, "reconnect_delay_max_s": 5.0}}
        )


def test_tls_insecure_requires_tls():
    with pytest.raises(ValidationError, match="tls_insecure"):
        MqttConf.model_validate({"broker": {"tls": False, "tls_insecure": True}})


def test_client_id_gets_the_bridge_discriminator():
    conf = MqttConf()
    assert conf.client_id_for(None) == "nd45-dtsu666"
    assert conf.client_id_for("nd45") == "nd45-dtsu666-nd45"


def test_two_systemd_instances_get_different_client_ids():
    # MQTT allows one connection per client id: sharing one would have the broker
    # evict the incumbent, which reconnects and evicts the newcomer, forever.
    conf = load_mqtt_config(MQTT_PATH)
    assert conf.client_id_for("nd45") != conf.client_id_for("etango")


# --- topics ----------------------------------------------------------------


def test_measurement_and_status_topics_are_namespaced_per_bridge():
    assert mqtt_publisher.measurement_topic("p", "nd45") == "p/nd45/measurements"
    assert mqtt_publisher.status_topic("p", "etango") == "p/etango/status"


def test_availability_topic_is_per_bridge_when_one_bridge_owns_the_process():
    # Otherwise each instance's Will would overwrite the other's "online".
    assert mqtt_publisher.availability_topic("p", "nd45") == "p/nd45/availability"
    assert mqtt_publisher.availability_topic("p", "etango") == "p/etango/availability"


def test_availability_topic_is_process_wide_when_every_bridge_runs():
    assert mqtt_publisher.availability_topic("p", None) == "p/availability"


def test_build_source_uses_each_bridges_own_freshness_threshold():
    config = load_config("config/config.json")
    source = build_source(_conf(), _runtimes(config), only=None)
    ages = {b.name: b.gate.max_age for b in source.bridges}
    for spec in config.bridge_specs:
        assert ages[spec.name] == spec.safety.max_data_age_s


class _FakeRuntime:
    """Minimal stand-in for app.BridgeRuntime: build_source reads name/store/spec."""

    def __init__(self, spec) -> None:
        self.name = spec.name
        self.spec = spec
        self.store = CanonicalStore()


def _runtimes(config):
    return [_FakeRuntime(spec) for spec in config.bridge_specs]


# --- payload ---------------------------------------------------------------


def test_payload_holds_ts_age_and_only_the_configured_points():
    bridge = _bridge(store=_fed({"p_total": 1234.5, "q_total": 7.0}, at=100.0))
    payload = json.loads(build_payload(bridge, ["p_total"], 100.0, 1_754_640_000.0))
    assert payload["p_total"] == 1234.5
    assert "q_total" not in payload
    assert set(payload) == {"ts", "age_s", "p_total"}


def test_ts_is_wall_clock_reconstructed_from_the_sample_age():
    # The store is stamped with the LOOP clock (seconds since process start).
    # Publishing that raw would hand a subscriber a 1970 timestamp.
    bridge = _bridge(store=_fed({"p_total": 1.0}, at=100.0))
    payload = json.loads(build_payload(bridge, ["p_total"], 101.0, 1_754_640_000.0))
    assert payload["ts"] == 1_754_639_999.0
    assert payload["age_s"] == 1.0


def test_a_stale_bridge_publishes_nothing():
    bridge = _bridge(gate=HealthGate(3.0), store=_fed({"p_total": 1.0}, at=100.0))
    assert build_payload(bridge, ["p_total"], 103.5, 0.0) is None


def test_the_freshness_limit_is_inclusive_like_the_fail_safe():
    bridge = _bridge(gate=HealthGate(3.0), store=_fed({"p_total": 1.0}, at=100.0))
    assert build_payload(bridge, ["p_total"], 103.0, 0.0) is not None


def test_a_never_polled_bridge_publishes_nothing():
    assert build_payload(_bridge(), ["p_total"], 100.0, 0.0) is None


def test_publishing_follows_exactly_the_same_rule_as_the_fail_safe():
    gate = HealthGate(3.0)
    bridge = _bridge(gate=gate, store=_fed({"p_total": 1.0}, at=100.0))
    for now in (100.0, 101.5, 103.0, 103.001, 200.0):
        served = gate.should_serve(bridge.store.age(now))
        assert (build_payload(bridge, ["p_total"], now, 0.0) is not None) is served


def test_a_non_finite_value_is_omitted_and_the_rest_kept():
    # json.dumps would otherwise emit bare NaN, which is not valid JSON.
    bridge = _bridge(store=_fed({"p_total": math.nan, "q_total": 5.0}, at=100.0))
    payload = json.loads(build_payload(bridge, ["p_total", "q_total"], 100.0, 0.0))
    assert "p_total" not in payload
    assert payload["q_total"] == 5.0


def test_a_missing_point_is_omitted_rather_than_published_as_null():
    bridge = _bridge(store=_fed({"q_total": 5.0}, at=100.0))
    payload = json.loads(build_payload(bridge, ["p_total", "q_total"], 100.0, 0.0))
    assert "p_total" not in payload
    assert payload["q_total"] == 5.0


def test_a_missing_point_warns_once_not_at_the_publish_rate(caplog):
    bridge = _bridge(store=_fed({"q_total": 5.0}, at=100.0))
    with caplog.at_level("WARNING"):
        for _ in range(5):
            build_payload(bridge, ["p_total"], 100.0, 0.0)
    assert sum("does not report mqtt point" in r.message for r in caplog.records) == 1


# --- publish loop ----------------------------------------------------------


async def test_publishes_one_measurement_per_bridge_per_cycle():
    now = asyncio.get_running_loop().time()
    bridges = [
        _bridge(name="nd45", store=_fed({"p_total": 1.0}, at=now),
                measurement_topic="p/nd45/measurements", status_topic="p/nd45/status"),
        _bridge(name="etango", store=_fed({"p_total": 2.0}, at=now),
                measurement_topic="p/etango/measurements", status_topic="p/etango/status"),
    ]
    source = _source(bridges=bridges, conf=_conf(availability={"enabled": False}))
    stop_event = asyncio.Event()
    client = _stop_after(_FakeClient(), stop_event, publishes=6)

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=lambda _s: client),
        timeout=5,
    )
    assert client.payloads_for("p/nd45/measurements")
    assert client.payloads_for("p/etango/measurements")
    # Status is edge-triggered, so exactly one per bridge across the whole run.
    assert client.topics().count("p/nd45/status") == 1
    assert client.topics().count("p/etango/status") == 1


async def test_publishes_online_on_connect_and_offline_on_a_clean_stop():
    source = _source(bridges=[_live_bridge()])
    stop_event = asyncio.Event()
    client = _stop_after(_FakeClient(), stop_event, publishes=3)

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=lambda _s: client),
        timeout=5,
    )
    avail = client.payloads_for("nd45-dtsu666/nd45/availability")
    assert avail[0] == "online"
    assert avail[-1] == "offline"


async def test_availability_is_retained_so_a_late_subscriber_can_read_it():
    source = _source(bridges=[_live_bridge()])
    stop_event = asyncio.Event()
    client = _stop_after(_FakeClient(), stop_event, publishes=3)

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=lambda _s: client),
        timeout=5,
    )
    for topic, _payload, _qos, retain in client.published:
        if topic.endswith("/availability") or topic.endswith("/status"):
            assert retain is True
        if topic.endswith("/measurements"):
            assert retain is False


async def test_a_stale_bridge_publishes_status_but_no_measurement():
    # Data older than the bridge's own max_data_age_s: the fail-safe has already
    # silenced its DTSU output, so MQTT must go quiet too.
    source = _source(bridges=[_bridge()])  # never polled -> age is inf
    stop_event = asyncio.Event()
    client = _stop_after(_FakeClient(), stop_event, publishes=2)

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=lambda _s: client),
        timeout=5,
    )
    assert "nd45-dtsu666/nd45/measurements" not in client.topics()
    assert client.payloads_for("nd45-dtsu666/nd45/status") == [STATUS_STALE]


async def test_status_is_edge_triggered_across_a_stale_transition():
    bridge = _live_bridge()
    source = _source(bridges=[bridge], conf=_conf(availability={"enabled": False}))
    stop_event = asyncio.Event()
    client = _FakeClient()
    inner = client.publish
    seen = {"n": 0}

    async def _publish(topic, payload, qos=0, retain=False, timeout=None):
        await inner(topic, payload, qos=qos, retain=retain, timeout=timeout)
        if topic.endswith("/measurements"):
            seen["n"] += 1
            if seen["n"] == 2:
                bridge.gate = HealthGate(-1.0)  # force everything stale
            elif seen["n"] >= 3:
                stop_event.set()
        if topic.endswith("/status") and payload == STATUS_STALE:
            bridge.gate = HealthGate(1e9)  # fresh again

    client.publish = _publish
    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=lambda _s: client),
        timeout=5,
    )
    assert client.payloads_for("nd45-dtsu666/nd45/status") == [
        STATUS_FRESH,
        STATUS_STALE,
        STATUS_FRESH,
    ]


async def test_reconnects_after_a_broker_failure_instead_of_raising():
    source = _source(
        bridges=[_live_bridge()],
        conf=_conf(broker={"reconnect_delay_s": 0.01, "reconnect_delay_max_s": 0.02}),
    )
    stop_event = asyncio.Event()
    good = _stop_after(_FakeClient(), stop_event, publishes=3)
    attempts = {"n": 0}

    def _factory(_source):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _Boom("broker down")
        return good

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=_factory),
        timeout=5,
    )
    assert attempts["n"] == 2
    # The second connection re-announces itself; the broker's view was reset by
    # the dropped session, so "online" must be sent again.
    assert good.payloads_for("nd45-dtsu666/nd45/availability")[0] == "online"


async def test_a_reconnect_forgets_the_status_edge_so_it_is_republished():
    bridge = _live_bridge(last_status=STATUS_FRESH)
    source = _source(
        bridges=[bridge],
        conf=_conf(broker={"reconnect_delay_s": 0.01, "reconnect_delay_max_s": 0.02}),
    )
    stop_event = asyncio.Event()
    good = _stop_after(_FakeClient(), stop_event, publishes=3)
    attempts = {"n": 0}

    def _factory(_source):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _Boom("broker down")
        return good

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=_factory),
        timeout=5,
    )
    assert good.payloads_for("nd45-dtsu666/nd45/status") == [STATUS_FRESH]


async def test_the_loop_never_raises_while_the_broker_is_always_down():
    source = _source(
        conf=_conf(broker={"reconnect_delay_s": 0.01, "reconnect_delay_max_s": 0.02})
    )
    stop_event = asyncio.Event()

    def _factory(_source):
        raise _Boom("still down")

    task = asyncio.create_task(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=_factory)
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # backing off, not dead
    stop_event.set()
    await asyncio.wait_for(task, timeout=5)  # returns, does not raise


async def test_the_backoff_grows_and_is_capped(monkeypatch):
    delays: list[float] = []
    source = _source(
        conf=_conf(broker={"reconnect_delay_s": 1.0, "reconnect_delay_max_s": 4.0})
    )
    stop_event = asyncio.Event()

    async def _fake_wait(event, delay):
        delays.append(delay)
        return len(delays) >= 5  # stop after five attempts

    monkeypatch.setattr(mqtt_publisher, "_wait", _fake_wait)

    def _factory(_source):
        raise _Boom("down")

    await asyncio.wait_for(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=_factory),
        timeout=5,
    )
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_the_stop_event_ends_the_loop_promptly():
    # Guards _wait being used rather than a bare sleep: a 60s interval must not
    # mean a 60s shutdown.
    source = _source(
        bridges=[_live_bridge()],
        conf=_conf(publish_interval_s=60.0),
    )
    stop_event = asyncio.Event()
    client = _FakeClient()
    task = asyncio.create_task(
        mqtt_publisher.publish_loop(source, stop_event, client_factory=lambda _s: client)
    )
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=5)


# --- start / stop ----------------------------------------------------------


def test_start_returns_none_when_disabled_or_absent():
    stop_event = asyncio.Event()
    config = load_config("config/config.json")
    assert mqtt_publisher.start(None, _runtimes(config), stop_event) is None
    assert mqtt_publisher.start(MqttConf(), _runtimes(config), stop_event) is None


def test_start_returns_none_when_there_are_no_bridges():
    assert mqtt_publisher.start(_conf(), [], asyncio.Event()) is None


async def test_start_survives_aiomqtt_being_uninstalled(monkeypatch, caplog):
    # An upgrade that skipped `pip install -e .` must lose MQTT, not the bridge.
    import sys

    monkeypatch.setitem(sys.modules, "aiomqtt", None)
    config = load_config("config/config.json")
    with caplog.at_level("ERROR"):
        task = mqtt_publisher.start(_conf(), _runtimes(config), asyncio.Event())
    assert task is None
    assert any("aiomqtt is not installed" in r.message for r in caplog.records)


async def test_start_and_stop_round_trip():
    config = load_config("config/config.json")
    stop_event = asyncio.Event()
    task = mqtt_publisher.start(
        _conf(broker={"host": "127.0.0.1", "port": 1}), _runtimes(config), stop_event,
        only="nd45",
    )
    assert task is not None
    await mqtt_publisher.stop(task)
    assert task.cancelled() or task.done()


async def test_stop_is_a_noop_for_none():
    await mqtt_publisher.stop(None)
