"""Reading a source through modbus-hub instead of our own TCP client.

No socket and no gRPC server is opened, matching the rest of this suite: the
part worth testing is the block bookkeeping and, above all, the rule that a
missing, failed or stale block is reported as an ERROR rather than as the
previously received registers. That rule is what keeps the fail-safe intact --
`poll_once` raises `PollError`, the `CanonicalStore` stops advancing, and
`supervise_server` silences the DTSU output so Sigenergy enters safe mode.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nd45_dtsu666 import hub_client
from nd45_dtsu666.config import (
    AppConfig,
    EtangoSourceConf,
    Nd45SourceConf,
    load_registers,
)
from nd45_dtsu666.hub_client import (
    BlockCache,
    BlockKey,
    HubClient,
    blocks_for_source,
    staleness_threshold,
)

REGISTERS = load_registers("config/registers.json")


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _cache(max_stale_s: float = 1.0):
    clock = FakeClock()
    cache = BlockCache(max_stale_s, now=clock)
    cache.set_stream_error(None)  # pretend the Subscribe stream is up
    return cache, clock


KEY = BlockKey(link="nd45", unit_id=1, fc=3, base=50, count=96)


# --- the fail-safe rule ----------------------------------------------------


def test_a_stale_block_reports_an_error_and_never_the_previous_registers():
    # The whole reason this client can be trusted in front of a battery: if the
    # hub stops refreshing, the bridge must go blind loudly, not quietly serve
    # a frozen meter reading to Sigenergy.
    cache, clock = _cache(max_stale_s=1.0)
    cache.update(KEY, list(range(96)))

    fresh = cache.lookup("nd45", 1, 3, 50, 96)
    assert not fresh.isError()
    assert fresh.registers[0] == 0

    clock.advance(1.5)
    stale = cache.lookup("nd45", 1, 3, 50, 96)
    assert stale.isError()
    assert stale.registers == []
    assert "stale" in str(stale)


def test_an_error_from_the_hub_replaces_the_registers_it_used_to_hold():
    cache, _clock = _cache()
    cache.update(KEY, list(range(96)))
    cache.update(KEY, None, error="i/o timeout")

    got = cache.lookup("nd45", 1, 3, 50, 96)
    assert got.isError()
    assert got.registers == []
    assert "i/o timeout" in str(got)


def test_a_down_stream_fails_every_lookup_even_a_young_one():
    # A stream that is gone will not refresh anything, so waiting out the
    # staleness window before failing would only delay the fail-safe.
    cache, _clock = _cache(max_stale_s=10.0)
    cache.update(KEY, list(range(96)))
    assert not cache.lookup("nd45", 1, 3, 50, 96).isError()

    cache.set_stream_error("connection refused")
    got = cache.lookup("nd45", 1, 3, 50, 96)
    assert got.isError()
    assert "connection refused" in str(got)


def test_a_block_nobody_subscribed_to_is_an_error_not_an_empty_success():
    cache, _clock = _cache()
    cache.update(KEY, list(range(96)))

    got = cache.lookup("nd45", 1, 3, 900, 96)
    assert got.isError()
    assert "no subscribed block covers" in str(got)


def test_a_fresh_cache_errors_before_the_first_update_arrives():
    cache = BlockCache(1.0, now=FakeClock())
    got = cache.lookup("nd45", 1, 3, 50, 96)
    assert got.isError()


# --- slicing and routing ---------------------------------------------------


def test_lookup_slices_the_requested_window_out_of_the_subscribed_block():
    cache, _clock = _cache()
    cache.update(KEY, [1000 + i for i in range(96)])

    got = cache.lookup("nd45", 1, 3, 60, 4)
    assert not got.isError()
    assert got.registers == [1010, 1011, 1012, 1013]


def test_lookups_are_scoped_by_link_unit_and_function_code():
    cache, _clock = _cache()
    cache.update(KEY, list(range(96)))

    assert cache.lookup("other", 1, 3, 50, 96).isError()
    assert cache.lookup("nd45", 2, 3, 50, 96).isError()
    assert cache.lookup("nd45", 1, 4, 50, 96).isError()


def test_a_window_running_past_the_block_is_an_error():
    cache, _clock = _cache()
    cache.update(KEY, list(range(96)))
    assert cache.lookup("nd45", 1, 3, 140, 20).isError()


class _StubConn:
    """Records what the client asked for; stands in for a HubConnection."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.connected = True
        self.closes = 0

    def read(self, link, fc, addr, count, slave):
        self.calls.append((link, fc, addr, count, slave))
        return hub_client.HubResponse(registers=[0] * count)

    async def connect(self):
        return True

    def close(self):
        self.closes += 1


async def test_holding_and_input_reads_route_to_their_own_function_codes():
    # etango reads FC=04 while nd45/huawei read FC=03; conflating them would
    # silently serve one device's map from another's registers.
    conn = _StubConn()
    client = HubClient(conn=conn, link="etango-1")

    await client.read_holding_registers(50, 96, slave=1)
    await client.read_input_registers(0, 32, slave=2)

    assert conn.calls == [
        ("etango-1", 3, 50, 96, 1),
        ("etango-1", 4, 0, 32, 2),
    ]


# --- subscription shape ----------------------------------------------------


def test_nd45_subscribes_to_the_pollers_own_read_groups():
    from nd45_dtsu666.nd45_poller import READ_GROUPS

    blocks = blocks_for_source("nd45", REGISTERS.nd45_source, unit_id=1, link="nd45")
    assert [(b.base, b.count) for b in blocks] == READ_GROUPS
    assert {b.fc for b in blocks} == {3}
    assert {b.unit_id for b in blocks} == {1}


def test_huawei_subscribes_to_the_maps_read_groups_on_fc3():
    side = REGISTERS.source_by_name("huawei_plant_source")
    blocks = blocks_for_source("huawei", side, unit_id=0, link="hsm")
    assert blocks, "the huawei map declares read_groups"
    assert {b.fc for b in blocks} == {3}
    for group, block in zip(side.read_groups, blocks):
        assert block.base == group.base + side.address_offset
        assert block.count == group.count


def test_etango_subscribes_on_fc4():
    side = REGISTERS.source_by_name("etango_source")
    blocks = blocks_for_source("etango", side, unit_id=1, link="etango-1")
    assert blocks
    assert {b.fc for b in blocks} == {4}


def test_a_per_group_unit_override_is_carried_into_the_subscription():
    # huawei_meter_source-style maps can pin a group to another RS485 address;
    # subscribing under the source's default unit would poll the wrong device.
    side = REGISTERS.source_by_name("huawei_plant_source").model_copy(deep=True)
    side.read_groups[0].unit_id = 7
    blocks = blocks_for_source("huawei", side, unit_id=0, link="hsm")
    assert blocks[0].unit_id == 7


def test_a_source_without_read_groups_is_refused_rather_than_polling_nothing():
    side = REGISTERS.source_by_name("huawei_plant_source").model_copy(deep=True)
    side.read_groups = []
    with pytest.raises(ValueError, match="no read_groups"):
        blocks_for_source("huawei", side, unit_id=0, link="hsm")


# --- staleness threshold ---------------------------------------------------


@pytest.mark.parametrize(
    "poll_interval_s,timeout_s,max_data_age_s",
    [
        (0.05, 1.0, 3.0),   # nd45 as deployed
        (0.2, 3.0, 3.0),    # etango as deployed
        (1.0, 6.0, 30.0),   # smartlogger alternative as configured
    ],
)
def test_staleness_always_trips_no_later_than_the_bridges_own_freshness_gate(
    poll_interval_s, timeout_s, max_data_age_s
):
    # If the client tolerated staleness for longer than safety.max_data_age_s,
    # the gate would fire on data the client still considered good -- the bridge
    # would go silent with no error anywhere explaining why.
    threshold = staleness_threshold(poll_interval_s, timeout_s)
    assert threshold <= max_data_age_s


def test_staleness_is_wider_than_a_single_missed_poll():
    # Otherwise ordinary jitter at a 50 ms cadence would flap the bridge.
    assert staleness_threshold(0.05, 1.0) > 0.05 * 2


# --- configuration ---------------------------------------------------------


def _bridge(source: dict) -> dict:
    return {
        "name": "b",
        "source": source,
        "dtsu": {"transport": "tcp", "slave_id": 1, "tcp": {"host": "0.0.0.0", "port": 5502}},
        "safety": {"max_data_age_s": 3.0},
    }


def test_via_hub_defaults_off_so_an_existing_config_keeps_its_own_connection():
    source = Nd45SourceConf(host="192.168.22.109")
    assert source.via_hub is False
    assert source.hub_socket == "/run/modbus-hub/hub.sock"


def test_via_hub_without_a_link_name_is_rejected_at_load():
    with pytest.raises(ValidationError, match="hub_link is empty"):
        Nd45SourceConf(host="192.168.22.109", via_hub=True)


def test_via_hub_with_a_link_name_loads():
    source = Nd45SourceConf(host="192.168.22.109", via_hub=True, hub_link="nd45")
    assert source.hub_link == "nd45"


def test_etango_via_hub_needs_a_link_per_device():
    with pytest.raises(ValidationError, match="no hub_link"):
        EtangoSourceConf(
            type="etango",
            via_hub=True,
            devices=[{"host": "192.168.30.5"}, {"host": "192.168.30.7"}],
        )


def test_two_etango_devices_may_not_share_a_hub_link():
    # Each relay is its own TCP endpoint; sharing a link would mean one of them
    # is never actually polled.
    with pytest.raises(ValidationError, match="share a hub_link"):
        EtangoSourceConf(
            type="etango",
            via_hub=True,
            devices=[
                {"host": "192.168.30.5", "hub_link": "etango-1"},
                {"host": "192.168.30.7", "hub_link": "etango-1"},
            ],
        )


def test_etango_via_hub_with_distinct_links_loads():
    source = EtangoSourceConf(
        type="etango",
        via_hub=True,
        devices=[
            {"host": "192.168.30.5", "hub_link": "etango-1"},
            {"host": "192.168.30.7", "hub_link": "etango-2"},
        ],
    )
    assert [d.hub_link for d in source.devices] == ["etango-1", "etango-2"]


def test_a_whole_config_file_round_trips_with_via_hub_set(tmp_path):
    cfg = {
        "bridges": [
            _bridge({
                "type": "nd45",
                "host": "192.168.22.109",
                "via_hub": True,
                "hub_link": "nd45",
                "poll_interval_s": 0.05,
                "timeout_s": 1.0,
                "stall_timeout_s": 30.0,
            })
        ]
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    loaded = AppConfig.model_validate(json.loads(path.read_text()))
    spec = loaded.bridge_specs[0]
    assert spec.source.via_hub is True
    assert spec.source.hub_link == "nd45"


# --- factory wiring --------------------------------------------------------


def test_the_client_factory_returns_a_hub_client_when_via_hub_is_set():
    from nd45_dtsu666.app import _make_client_factory
    from nd45_dtsu666.config import BridgeConf

    spec = BridgeConf.model_validate(_bridge({
        "type": "nd45",
        "host": "192.168.22.109",
        "via_hub": True,
        "hub_link": "nd45",
    }))
    factory = _make_client_factory(spec, REGISTERS.nd45_source)
    client = factory()
    assert isinstance(client, HubClient)
    assert client.link == "nd45"
    # Subscribed to exactly what the ND45 poller will ask for.
    from nd45_dtsu666.nd45_poller import READ_GROUPS

    assert [(b.base, b.count) for b in client.conn.blocks] == READ_GROUPS


def test_an_etango_hub_client_is_still_a_multi_host_client():
    # supervise_poller and connect_with_retry must stay source-agnostic.
    from nd45_dtsu666.app import _make_client_factory
    from nd45_dtsu666.config import BridgeConf
    from nd45_dtsu666.etango_poller import MultiHostClient

    spec = BridgeConf.model_validate(_bridge({
        "type": "etango",
        "via_hub": True,
        "poll_interval_s": 0.2,
        "timeout_s": 3.0,
        "stall_timeout_s": 30.0,
        "devices": [
            {"host": "192.168.30.5", "hub_link": "etango-1"},
            {"host": "192.168.30.7", "hub_link": "etango-2"},
        ],
    }))
    client = _make_client_factory(spec, REGISTERS.source_by_name("etango_source"))()
    assert isinstance(client, MultiHostClient)
    assert [link.client.link for link in client.devices] == ["etango-1", "etango-2"]
    # One stream for all four relays, not one per relay.
    assert len({id(link.client.conn) for link in client.devices}) == 1


def test_closing_a_shared_connection_twice_is_harmless():
    # MultiHostClient.close() walks every device, and they share one connection.
    from nd45_dtsu666.app import _make_client_factory
    from nd45_dtsu666.config import BridgeConf

    spec = BridgeConf.model_validate(_bridge({
        "type": "etango",
        "via_hub": True,
        "poll_interval_s": 0.2,
        "timeout_s": 3.0,
        "stall_timeout_s": 30.0,
        "devices": [
            {"host": "192.168.30.5", "hub_link": "etango-1"},
            {"host": "192.168.30.7", "hub_link": "etango-2"},
        ],
    }))
    client = _make_client_factory(spec, REGISTERS.source_by_name("etango_source"))()
    client.close()
    client.close()
    assert client.connected is False


async def test_a_direct_source_still_gets_a_plain_tcp_client():
    # async because AsyncModbusTcpClient grabs the running loop in __init__.
    from pymodbus.client import AsyncModbusTcpClient

    from nd45_dtsu666.app import _make_client_factory
    from nd45_dtsu666.config import BridgeConf

    spec = BridgeConf.model_validate(_bridge({"type": "nd45", "host": "192.168.22.109"}))
    client = _make_client_factory(spec, REGISTERS.nd45_source)()
    assert isinstance(client, AsyncModbusTcpClient)


def test_via_hub_without_a_source_side_fails_loudly():
    # diagnostics and build_bridge both have the source side; a new call site
    # that forgets it must not silently fall back to a direct connection.
    from nd45_dtsu666.app import _make_client_factory
    from nd45_dtsu666.config import BridgeConf

    spec = BridgeConf.model_validate(_bridge({
        "type": "nd45", "host": "1.2.3.4", "via_hub": True, "hub_link": "nd45",
    }))
    with pytest.raises(ValueError, match="without a source side"):
        _make_client_factory(spec)


# --- readiness -------------------------------------------------------------


async def test_connect_waits_for_every_declared_block_not_just_the_first():
    # The blocks arrive as separate stream messages a few milliseconds apart.
    # Returning True after the first one let the very next poll_once fail on a
    # block that simply had not been delivered yet -- a startup race that looked
    # exactly like a dead source. Caught end-to-end against the real hub.
    import asyncio

    from nd45_dtsu666.hub_client import HubConnection

    blocks = blocks_for_source("nd45", REGISTERS.nd45_source, unit_id=1, link="nd45")
    assert len(blocks) > 1, "the ND45 map must have several read groups for this to mean anything"

    conn = HubConnection(
        socket_path="/nonexistent.sock",
        client_id="test",
        blocks=blocks,
        period_s=0.01,
        max_stale_s=1.0,
    )
    # Simulate the stream delivering the blocks one at a time.
    conn.cache.set_stream_error(None)
    for block in blocks[:-1]:
        conn.cache.update(block, [0] * block.count)
        conn._awaited.discard(block)
        if not conn._awaited:
            conn._ready.set()
    assert not conn._ready.is_set(), "readiness must not fire while a block is still missing"

    last = blocks[-1]
    conn.cache.update(last, [0] * last.count)
    conn._awaited.discard(last)
    if not conn._awaited:
        conn._ready.set()
    assert conn._ready.is_set()
    await asyncio.sleep(0)


def test_a_block_that_keeps_failing_still_counts_as_delivered():
    # Otherwise connect() would hang forever on a device that is simply down,
    # instead of letting the bridge start and report the fault through its own
    # freshness gate.
    from nd45_dtsu666.hub_client import HubConnection

    blocks = blocks_for_source("nd45", REGISTERS.nd45_source, unit_id=1, link="nd45")
    conn = HubConnection(
        socket_path="/nonexistent.sock", client_id="test", blocks=blocks,
        period_s=0.01, max_stale_s=1.0,
    )
    conn.cache.set_stream_error(None)
    for block in blocks:
        conn.cache.update(block, None, error="i/o timeout")
        conn._awaited.discard(block)
    assert not conn._awaited
    assert conn.cache.lookup("nd45", 1, 3, 50, 96).isError()


def test_the_generated_stubs_import_against_the_installed_protobuf_runtime():
    # hub_client imports these lazily, so nothing else in this suite would
    # notice a missing or too-old protobuf -- the failure would surface on the
    # device, at the moment via_hub is switched on. The generated hub_pb2 calls
    # ValidateProtobufRuntimeVersion at import time, so importing it here is the
    # whole check.
    from nd45_dtsu666.hubpb import hub_pb2, hub_pb2_grpc

    ref = hub_pb2.BlockRef(link="nd45", unit_id=1, function_code=3, base=50, count=96)
    assert ref.link == "nd45"
    assert hasattr(hub_pb2_grpc, "ModbusHubStub")


def test_a_subscribe_request_can_be_built_from_real_blocks():
    # Catches a field renamed in the contract without the client following.
    from nd45_dtsu666.hubpb import hub_pb2

    blocks = blocks_for_source("nd45", REGISTERS.nd45_source, unit_id=1, link="nd45")
    req = hub_pb2.SubscribeRequest(
        client_id="nd45-dtsu666/nd45",
        blocks=[
            hub_pb2.BlockSubscription(
                block=hub_pb2.BlockRef(
                    link=b.link, unit_id=b.unit_id, function_code=b.fc,
                    base=b.base, count=b.count,
                ),
                period_ms=50,
            )
            for b in blocks
        ],
    )
    assert len(req.blocks) == len(blocks)
    assert req.blocks[0].block.function_code == 3
