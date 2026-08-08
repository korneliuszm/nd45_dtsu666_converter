"""Periodic MQTT publication of canonical measurements.

One client per PROCESS, publishing every bridge that process runs under
`<topic_prefix>/<bridge>/measurements`. Configured by its own file,
`config/mqtt.json` -- see `config.MqttConf` for why it is not part of config.json.

Driven by its own timer, never by the poll loop. `on_update` (app._bridge_coros)
is a synchronous callback on the poll loop's critical path, where its ordering is
already load-bearing and where a broker round-trip has no business being; and
ND45 polls at 20 Hz, so an update-driven publisher would emit 20 msg/s per bridge
and make the configured interval meaningless. Sampling the store on a separate
timer decouples the two: a bridge slower than the publish interval simply repeats
a sample, which is visible to a subscriber because the payload `ts` belongs to the
sample, not to the message.

THREE CLOCKS, and mixing them is the easy mistake here. Data age uses the asyncio
LOOP clock, because that is what the pollers stamp into the store
(nd45_poller.poll loop -> `on_update(values, loop.time())`). Scheduling uses the
loop clock too. The payload `ts` is WALL clock, because a loop-clock number is
seconds-since-process-start and means nothing to a subscriber -- it is derived as
`wall_now - age` and never read from the store. `build_payload` takes both clocks
as arguments and calls neither, so it is testable without a running loop, the same
discipline as `metrics.render`.

aiomqtt is imported lazily. An install that upgraded the checkout without re-running
`pip install -e .` must lose MQTT, not the bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field

from .canonical import CanonicalStore, HealthGate
from .config import MqttConf

log = logging.getLogger(__name__)

STATUS_FRESH = "fresh"
STATUS_STALE = "stale"


@dataclass
class BridgePublish:
    """One bridge's publishable state. Holds references, never copies."""

    name: str
    store: CanonicalStore
    # Built from the same safety.max_data_age_s supervise_server uses. Publishing
    # a value the DTSU output is refusing to serve would put a number on a
    # dashboard that Sigenergy is explicitly not being given.
    gate: HealthGate
    measurement_topic: str
    status_topic: str
    # Edge tracking, so a steady bridge does not emit five status messages a second.
    last_status: str | None = None
    # Points this bridge has already been warned about, so a source that simply
    # does not report one logs once rather than at the publish rate.
    warned: set[str] = field(default_factory=set)


@dataclass
class MqttSource:
    """Everything the publisher reads. Holds references, never copies."""

    conf: MqttConf
    client_id: str
    availability_topic: str
    bridges: list[BridgePublish] = field(default_factory=list)


# --- topics -----------------------------------------------------------------


def measurement_topic(prefix: str, bridge_name: str) -> str:
    return f"{prefix}/{bridge_name}/measurements"


def status_topic(prefix: str, bridge_name: str) -> str:
    return f"{prefix}/{bridge_name}/status"


def availability_topic(prefix: str, only: str | None) -> str:
    """Topic the process-level Last Will covers.

    Discriminated by `only` for the same reason the client id is: the two systemd
    instances share a broker, and a single `<prefix>/availability` would have each
    one's Will overwrite the other's `online`. Running every bridge in one process
    (dev, `monitor`) there is exactly one publisher, so the bare topic is right.
    """
    return f"{prefix}/availability" if only is None else f"{prefix}/{only}/availability"


def build_source(conf: MqttConf, bridges, only: str | None = None) -> MqttSource:
    """Pure: a list of BridgeRuntime -> the publisher's view of them."""
    prefix = conf.topic_prefix
    return MqttSource(
        conf=conf,
        client_id=conf.client_id_for(only),
        availability_topic=availability_topic(prefix, only),
        bridges=[
            BridgePublish(
                name=bridge.name,
                store=bridge.store,
                gate=HealthGate(bridge.spec.safety.max_data_age_s),
                measurement_topic=measurement_topic(prefix, bridge.name),
                status_topic=status_topic(prefix, bridge.name),
            )
            for bridge in bridges
        ],
    )


# --- payload ----------------------------------------------------------------


def build_payload(
    bridge: BridgePublish,
    points: list[str],
    loop_now: float,
    wall_now: float,
) -> str | None:
    """JSON for one bridge, or None when its data is too old to publish.

    Pure: both clocks are arguments. `loop_now` decides freshness, because the
    store is stamped with loop.time(); `wall_now` only turns the sample's age into
    an epoch timestamp a subscriber can read. Deriving `ts` as `wall_now - age`
    rather than calling time.time() at publish time keeps the timestamp on the
    *sample* -- so a source slower than the publish interval shows up as a
    repeated `ts`, not as fresh-looking data.
    """
    age = bridge.store.age(loop_now)
    # The same rule and the same threshold as the fail-safe, deliberately: MQTT
    # goes quiet exactly when supervise_server silences this bridge's output.
    # age is math.inf before the first sample, so a never-polled bridge is covered.
    if not bridge.gate.should_serve(age):
        return None
    values, _loop_ts = bridge.store.snapshot()
    payload: dict[str, float] = {"ts": round(wall_now - age, 3), "age_s": round(age, 3)}
    missing: list[str] = []
    for name in points:
        value = values.get(name)
        if value is None:
            missing.append(name)
            continue
        # A NaN would serialise as bare `NaN`, which is not valid JSON and which
        # consumers variously reject or silently read as null. Omit it instead.
        if math.isfinite(value):
            payload[name] = value
    if missing:
        unwarned = [name for name in missing if name not in bridge.warned]
        if unwarned:
            bridge.warned.update(unwarned)
            log.warning(
                "bridge %r does not report mqtt point(s) %s; omitted from its payload",
                bridge.name,
                ", ".join(sorted(unwarned)),
            )
    return json.dumps(payload, separators=(",", ":"))


# --- publishing -------------------------------------------------------------


async def _wait(stop_event: asyncio.Event, delay: float) -> bool:
    """Sleep up to `delay`; True if a stop was requested. Same idiom as serve_metrics."""
    if delay <= 0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return False
    return True


def _build_client(source: MqttSource):
    """Construct the aiomqtt client.

    Must be called from inside the running loop: aiomqtt's Client.__init__ calls
    asyncio.get_running_loop(). That is why this is not done in start() or at
    config-load time.
    """
    import aiomqtt

    conf = source.conf
    broker = conf.broker
    will = None
    if conf.availability.enabled:
        will = aiomqtt.Will(
            topic=source.availability_topic,
            payload=conf.availability.offline,
            qos=conf.qos,
            retain=conf.availability.retain,
        )
    return aiomqtt.Client(
        hostname=broker.host,
        port=broker.port,
        username=broker.username or None,
        password=broker.password or None,
        # aiomqtt 2.x renamed this from `client_id`; 1.x spelling silently lands
        # in **kwargs and leaves the broker generating a random id per reconnect.
        identifier=source.client_id,
        keepalive=broker.keepalive_s,
        timeout=broker.connect_timeout_s,
        will=will,
        tls_params=aiomqtt.TLSParameters() if broker.tls else None,
        tls_insecure=broker.tls_insecure or None,
    )


async def _publish_availability(client, source: MqttSource, online: bool) -> None:
    conf = source.conf
    if not conf.availability.enabled:
        return
    payload = conf.availability.online if online else conf.availability.offline
    await client.publish(
        source.availability_topic,
        payload,
        qos=conf.qos,
        retain=conf.availability.retain,
        timeout=conf.publish_timeout_s,
    )


async def _publish_status(client, source: MqttSource, bridge: BridgePublish, status: str) -> None:
    """Retained fresh/stale, edge triggered."""
    if bridge.last_status == status:
        return
    conf = source.conf
    await client.publish(
        bridge.status_topic,
        status,
        qos=conf.qos,
        retain=True,
        timeout=conf.publish_timeout_s,
    )
    bridge.last_status = status
    log.info("bridge %r mqtt status -> %s", bridge.name, status)


async def _publish_cycles(client, source: MqttSource, stop_event: asyncio.Event) -> None:
    """One message per bridge per period, until the stop event."""
    loop = asyncio.get_running_loop()
    conf = source.conf
    interval = conf.publish_interval_s
    deadline = loop.time()
    while not stop_event.is_set():
        loop_now, wall_now = loop.time(), time.time()
        for bridge in source.bridges:
            payload = build_payload(bridge, conf.points, loop_now, wall_now)
            if payload is None:
                # Stale is not an error: the fail-safe has already silenced this
                # bridge's DTSU output, and republishing the last good number
                # would tell a dashboard the opposite of what Sigenergy is seeing.
                await _publish_status(client, source, bridge, STATUS_STALE)
                continue
            await client.publish(
                bridge.measurement_topic,
                payload,
                qos=conf.qos,
                retain=conf.retain,
                timeout=conf.publish_timeout_s,
            )
            await _publish_status(client, source, bridge, STATUS_FRESH)

        # Deadline-based, not sleep(interval): a fixed sleep adds the publish work
        # and the broker round-trip to every period, so the cadence walks off over
        # hours. Falling a whole period behind resynchronises rather than firing a
        # catch-up burst -- a backlog of 200ms-old measurements has no value.
        deadline += interval
        remaining = deadline - loop.time()
        if remaining <= 0:
            deadline = loop.time() + interval
            remaining = interval
        if await _wait(stop_event, remaining):
            return


async def publish_loop(
    source: MqttSource,
    stop_event: asyncio.Event,
    client_factory=_build_client,
) -> None:
    """Publish until `stop_event`. Never raises: any broker problem reconnects.

    Same contract as serve_metrics, and for a sharper reason: this task is not in
    `pipe.coros`, so nobody awaits it until shutdown and an escaping exception
    would leave a silently dead publisher behind a process that still looks healthy.

    aiomqtt 2.x does not reconnect on its own inside the context manager, so the
    reconnect is this outer loop -- the pattern its own docs prescribe -- with the
    same exponential backoff shape as app.connect_with_retry.
    """
    conf = source.conf
    delay = conf.broker.reconnect_delay_s
    while not stop_event.is_set():
        try:
            # A fresh client per attempt: the context manager is reusable but not
            # reentrant, and at a 2-30s backoff rebuilding it costs nothing.
            async with client_factory(source) as client:
                log.info(
                    "MQTT connected to %s:%d as %r, publishing %s every %.3fs",
                    conf.broker.host,
                    conf.broker.port,
                    source.client_id,
                    ",".join(conf.points),
                    conf.publish_interval_s,
                )
                delay = conf.broker.reconnect_delay_s  # reset the backoff
                await _publish_availability(client, source, online=True)
                await _publish_cycles(client, source, stop_event)
                await _publish_availability(client, source, online=False)
            return  # stop_event ended the cycle loop: clean shutdown
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - aiomqtt.MqttError, and anything paho leaks
            # Catching Exception rather than MqttError is deliberate. The
            # never-raises contract has to hold for whatever comes out of paho,
            # and it keeps the reconnect path testable without aiomqtt installed.
            log.warning("MQTT session ended (%r); reconnecting in %.1fs", exc, delay)
            log.debug("MQTT failure detail", exc_info=True)
        # Whatever the broker last heard from us is now unreliable: forget the
        # edge state so the next connection republishes every bridge's status.
        for bridge in source.bridges:
            bridge.last_status = None
        if await _wait(stop_event, delay):
            return
        delay = min(delay * 2, conf.broker.reconnect_delay_max_s)


def start(
    conf: MqttConf | None,
    bridges,
    stop_event: asyncio.Event,
    only: str | None = None,
) -> asyncio.Task | None:
    """Start the publisher as a task, or None when it is disabled or unavailable.

    Started by run_app *before* connect_with_retry, for the same reason the metrics
    endpoint is: a publisher that only comes up once the source connects is absent
    during exactly the outage its availability topic exists to report.

    `only` names the single bridge this process runs (how systemd starts it). It
    discriminates both the client id and the availability topic so two instances
    sharing one broker cannot evict each other.
    """
    if conf is None or not conf.enabled:
        return None
    if not bridges:
        return None
    try:
        import aiomqtt  # noqa: F401
    except ImportError:
        # Optional telemetry must never take the bridge down with it. This happens
        # when a checkout is upgraded without re-running the venv install.
        log.error(
            "mqtt.enabled is true but aiomqtt is not installed; MQTT publishing is "
            'off. Re-run `pip install -e ".[dev]"` in the venv.'
        )
        return None
    source = build_source(conf, bridges, only)
    return asyncio.create_task(publish_loop(source, stop_event))


async def stop(task: asyncio.Task | None) -> None:
    """Cancel a task from `start` and collect its outcome.

    Normally a no-op on an already-finished task: run_app sets the stop event, the
    coros return, and publish_loop has usually exited on its own by the time this
    is reached. When the cancel does land it pre-empts the explicit `offline`
    publish -- which is fine, because dropping the connection makes the broker
    deliver the retained Will, the same payload on the same topic.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 - best-effort shutdown logging
        log.warning("MQTT publisher raised while shutting down: %r", exc)
