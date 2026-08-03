import json
import math

import pytest
from pydantic import ValidationError

from nd45_dtsu666.config import (
    MAX_READ_REGISTERS,
    AppConfig,
    DeriveOp,
    DtsuConf,
    DtsuIdentityConf,
    DtsuRtuConf,
    DtsuTcpConf,
    HuaweiSourceConf,
    Nd45Conf,
    Nd45SourceConf,
    ReadGroup,
    RegisterMap,
    SafetyConf,
    SourcePoint,
    SourceSide,
    StaticDebugConf,
    TargetPoint,
    load_config,
    load_registers,
)


def test_source_point_rejects_both_addr_and_compose():
    with pytest.raises(ValidationError, match="both 'addr' and 'compose'"):
        SourcePoint(addr=50, compose=[900, 902], factors=[1000, 1])


def test_source_point_rejects_neither_addr_nor_compose():
    with pytest.raises(ValidationError, match="either 'addr' or 'compose'"):
        SourcePoint()


def test_source_point_rejects_factors_length_mismatch():
    with pytest.raises(ValidationError, match="'factors' length must match"):
        SourcePoint(compose=[900, 902], factors=[1000])


def test_source_point_allows_compose_without_factors():
    # factors is optional; poll_once defaults it to all-1.0
    pt = SourcePoint(compose=[900, 902])
    assert pt.factors is None


def test_load_registers_reads_seed(tmp_path):
    reg = load_registers("config/registers.json")
    assert reg.dtsu_target.function_code == 3
    assert reg.nd45_source.points["u_l1"].addr == 50
    assert reg.dtsu_target.points["u_l1"].addr == 8198
    assert reg.dtsu_target.points["u_l1"].from_ == "u_l1"
    assert reg.dtsu_target.points["u_l1"].scale == 10
    # energy is a two-register compose on the ND45 side
    assert reg.nd45_source.points["imp_energy_total"].compose == [912, 914]
    assert reg.nd45_source.points["imp_energy_total"].factors == [1000, 1]


def test_load_registers_reads_sigen_measurement_map():
    reg = load_registers("config/registers.json")
    sigen = reg.dtsu_sigen_ext_target

    assert sigen.function_code == 4
    assert sigen.word_order == "big"
    assert sigen.byte_order == "big"
    # power/reactive/apparent power are reported in kW/kvar/kVA (x0.001) on the
    # Sigen OEM map, unlike every other quantity which is direct SI (x1).
    power_names = {
        "p_total", "p_l1", "p_l2", "p_l3",
        "q_total", "q_l1", "q_l2", "q_l3",
        "s_total", "s_l1", "s_l2", "s_l3",
    }
    for name, point in sigen.points.items():
        classic = reg.dtsu_target.points[name]
        assert point.addr == classic.addr - 2806
        assert point.scale == (0.001 if name in power_names else 1)
        assert point.sign == classic.sign
        assert point.divide_by_ct is False  # already primary-side, unlike the classic map


def test_load_registers_classic_secondary_side_points_divide_by_ct():
    reg = load_registers("config/registers.json")
    classic = reg.dtsu_target

    ct_divided = {
        "i_l1", "i_l2", "i_l3",
        "p_total", "p_l1", "p_l2", "p_l3",
        "q_total", "q_l1", "q_l2", "q_l3",
        "s_total", "s_l1", "s_l2", "s_l3",
        "active_energy_coarse", "reactive_exp_energy_coarse",
        "reactive_imp_energy_coarse", "imp_ep", "imp_ep_l1",
        "imp_ep_l2", "imp_ep_l3", "net_imp_ep", "exp_ep",
        "exp_ep_l1", "exp_ep_l2", "exp_ep_l3", "net_exp_ep",
        "reactive_imp_energy_coarse_alias",
        "reactive_exp_energy_coarse_alias",
    }
    for name, point in classic.points.items():
        assert point.divide_by_ct == (name in ct_divided), name


def test_load_registers_reads_physical_energy_maps():
    reg = load_registers("config/registers.json")
    classic = reg.dtsu_target.points
    extended = reg.dtsu_sigen_ext_energy.points

    classic_expected = {
        "active_energy_coarse": (0x1000, "active_energy_total", True),
        "reactive_exp_energy_coarse": (
            0x100A, "reactive_exp_energy_total", True
        ),
        "reactive_imp_energy_coarse": (
            0x1014, "reactive_imp_energy_total", True
        ),
        "imp_ep": (0x101E, "imp_energy_total", False),
        "imp_ep_l1": (0x1020, "imp_energy_l1", False),
        "imp_ep_l2": (0x1022, "imp_energy_l2", False),
        "imp_ep_l3": (0x1024, "imp_energy_l3", False),
        "net_imp_ep": (0x1026, "net_imp_energy_total", False),
        "exp_ep": (0x1028, "exp_energy_total", False),
        "exp_ep_l1": (0x102A, "exp_energy_l1", False),
        "exp_ep_l2": (0x102C, "exp_energy_l2", False),
        "exp_ep_l3": (0x102E, "exp_energy_l3", False),
        "net_exp_ep": (0x1030, "net_exp_energy_total", False),
        "reactive_imp_energy_coarse_alias": (
            0x103C, "reactive_imp_energy_total", True
        ),
        "reactive_exp_energy_coarse_alias": (
            0x1050, "reactive_exp_energy_total", True
        ),
    }
    extended_expected = {
        "active_energy_coarse": (0x1800, "active_energy_total", True),
        "reactive_exp_energy_coarse": (
            0x180A, "reactive_exp_energy_total", True
        ),
        "reactive_imp_energy_coarse": (
            0x1814, "reactive_imp_energy_total", True
        ),
        "imp_ep": (0x181E, "imp_energy_total", False),
        "imp_ep_l1": (0x1820, "imp_energy_l1", False),
        "imp_ep_l2": (0x1822, "imp_energy_l2", False),
        "imp_ep_l3": (0x1824, "imp_energy_l3", False),
        "net_imp_ep": (0x1826, "net_imp_energy_total", False),
        "exp_ep": (0x1828, "exp_energy_total", False),
        "exp_ep_l1": (0x182A, "exp_energy_l1", False),
        "exp_ep_l2": (0x182C, "exp_energy_l2", False),
        "exp_ep_l3": (0x182E, "exp_energy_l3", False),
        "net_exp_ep": (0x1830, "net_exp_energy_total", False),
        "reactive_imp_energy_coarse_alias": (
            0x183C, "reactive_imp_energy_total", True
        ),
        "reactive_exp_energy_coarse_alias": (
            0x1850, "reactive_exp_energy_total", True
        ),
    }

    classic_energy = {
        name for name, point in classic.items() if 0x1000 <= point.addr <= 0x1051
    }
    assert classic_energy == set(classic_expected)
    assert set(extended) == set(extended_expected)
    for name, (addr, source, coarse) in classic_expected.items():
        point = classic[name]
        assert (point.addr, point.from_, point.zero_low_word) == (
            addr, source, coarse
        )
        assert point.divide_by_ct is True
    for name, (addr, source, coarse) in extended_expected.items():
        point = extended[name]
        assert (point.addr, point.from_, point.zero_low_word) == (
            addr, source, coarse
        )
        assert point.divide_by_ct is False

    assert "reactive_energy_total" not in reg.nd45_source.points


def test_load_registers_reads_sigen_identity():
    identity = load_registers("config/registers.json").dtsu_sigen_identity

    assert identity.function_code == 3
    model = identity.points["model_string"]
    assert model.addr == 0xF100
    assert model.type == "ascii"
    assert model.length == 20
    assert model.static_value == "Sigen Sensor TPX-CH\u0000"
    handshake = identity.points["handshake_magic"]
    assert handshake.addr == 0xF114
    assert handshake.type == "uint32"
    assert handshake.static_value == 5376


def test_load_config_reads_seed():
    cfg = load_config("config/config.json")
    nd45 = cfg.bridge_specs[0]
    assert nd45.name == "nd45"
    assert nd45.source.port == 502
    assert nd45.dtsu.slave_id == 10
    assert nd45.dtsu.transport == "rtu"
    assert nd45.dtsu.rtu.port == "/dev/ttyAMA2"
    assert nd45.dtsu.tcp is None  # RTU output; no unused tcp block to confuse readers
    assert nd45.safety.max_data_age_s == 3.0
    assert nd45.dtsu.identity.rev == 103
    assert nd45.dtsu.identity.ucode == 701
    assert nd45.dtsu.identity.ir_at == 200
    assert nd45.dtsu.identity.ur_at == 10
    assert cfg.static_debug.feed_interval_s == 0.5
    assert cfg.static_debug.values["u_l1"] == 240.0
    assert cfg.static_debug.values["p_total"] == -60000.0
    assert cfg.static_debug.values["pf_total"] == -0.95
    assert cfg.static_debug.values["s_total"] == 60300.0
    assert cfg.static_debug.values["imp_energy_total"] == 7.0
    assert cfg.static_debug.values["exp_energy_total"] == 3.0
    assert cfg.static_debug.values["exp_energy_l1"] == 0.8
    assert cfg.static_debug.values["exp_energy_l2"] == 1.0
    assert cfg.static_debug.values["exp_energy_l3"] == 1.0
    assert cfg.static_debug.values["reactive_imp_energy_total"] == 1.2
    assert cfg.static_debug.values["reactive_exp_energy_total"] == 2.8


def test_static_debug_rejects_unknown_value_name():
    with pytest.raises(ValidationError, match="unknown static debug value"):
        StaticDebugConf(values={"u_l1_typo": 230.0})


def test_static_debug_accepts_apparent_power_values():
    configured = StaticDebugConf(values={
        "s_l1": 499.0,
        "s_l2": 599.0,
        "s_l3": 699.0,
        "s_total": 1900.0,
    })

    assert configured.values["s_l1"] == 499.0
    assert configured.values["s_total"] == 1900.0


def test_static_debug_accepts_independent_directional_energy_values():
    configured = StaticDebugConf(values={
        "exp_energy_l1": 0.8,
        "exp_energy_l2": 1.0,
        "exp_energy_l3": 1.0,
        "reactive_imp_energy_total": 1.2,
        "reactive_exp_energy_total": 2.8,
    })
    assert configured.values["exp_energy_l1"] == 0.8
    assert configured.values["reactive_imp_energy_total"] == 1.2
    assert configured.values["reactive_exp_energy_total"] == 2.8


@pytest.mark.parametrize(
    "name",
    [
        "active_energy_total",
        "net_imp_energy_total",
        "net_exp_energy_total",
        "reactive_energy_total",
    ],
)
def test_static_debug_rejects_derived_or_obsolete_energy_inputs(name):
    with pytest.raises(ValidationError, match="unknown static debug value"):
        StaticDebugConf(values={name: 1.0})


@pytest.mark.parametrize("value", [True, "230.0", math.nan, math.inf, -math.inf])
def test_static_debug_rejects_non_numeric_or_non_finite_values(value):
    with pytest.raises(ValidationError, match="finite number"):
        StaticDebugConf(values={"u_l1": value})


def test_static_debug_feed_interval_must_be_shorter_than_max_data_age():
    with pytest.raises(ValidationError, match="feed_interval_s"):
        AppConfig(
            nd45=Nd45Conf(host="127.0.0.1"),
            dtsu=DtsuConf(
                transport="rtu",
                slave_id=1,
                rtu=DtsuRtuConf(port="/dev/null"),
            ),
            safety=SafetyConf(max_data_age_s=0.5),
            static_debug=StaticDebugConf(feed_interval_s=0.5),
        )


def test_target_point_defaults(tmp_path):
    reg = load_registers("config/registers.json")
    pf = reg.dtsu_target.points["pf_total"]
    assert pf.sign == 1
    assert pf.offset == 0
    assert pf.divide_by_ct is False
    assert pf.zero_low_word is False
    assert TargetPoint(addr=1, **{"from": "x"}).zero_low_word is False


def test_dtsu_identity_conf_rejects_non_positive_ir_at():
    with pytest.raises(ValidationError, match="ir_at"):
        DtsuIdentityConf(ir_at=0)


def test_reliability_defaults():
    nd45 = load_config("config/config.json").bridge_specs[0]
    assert nd45.source.reconnect_delay_s == 1.0
    assert nd45.source.reconnect_delay_max_s == 30.0
    assert nd45.safety.min_restart_interval_s == 5.0


def test_dtsu_conf_rtu_requires_rtu_block():
    with pytest.raises(ValidationError):
        DtsuConf(transport="rtu", slave_id=1)


def test_dtsu_conf_tcp_requires_tcp_block():
    with pytest.raises(ValidationError):
        DtsuConf(transport="tcp", slave_id=1)


def test_dtsu_conf_rtu_builds_with_rtu_block():
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/ttyAMA2"))
    assert cfg.rtu.baudrate == 9600
    assert cfg.tcp is None


def test_dtsu_conf_tcp_builds_with_tcp_block():
    cfg = DtsuConf(transport="tcp", slave_id=1, tcp=DtsuTcpConf())
    assert cfg.tcp.host == "0.0.0.0"
    assert cfg.tcp.port == 502


def test_dtsu_identity_conf_defaults_match_legacy_static_registers():
    identity = DtsuIdentityConf()
    assert identity.rev == 100
    assert identity.ucode == 0
    assert identity.clr_e == 0
    assert identity.net == 0
    assert identity.ir_at == 10
    assert identity.ur_at == 10
    assert identity.disp == 0
    assert identity.b_lcd == 0
    assert identity.endian == 0
    assert identity.protocol == 0


def test_dtsu_conf_identity_defaults_when_omitted():
    cfg = DtsuConf(transport="rtu", slave_id=1, rtu=DtsuRtuConf(port="/dev/ttyAMA2"))
    assert cfg.identity.ir_at == 10
    assert cfg.identity.ur_at == 10


def test_dtsu_conf_identity_accepts_overrides():
    cfg = DtsuConf(
        transport="rtu",
        slave_id=1,
        rtu=DtsuRtuConf(port="/dev/ttyAMA2"),
        identity=DtsuIdentityConf(rev=103, ucode=701, ir_at=200, ur_at=10),
    )
    assert cfg.identity.rev == 103
    assert cfg.identity.ucode == 701
    assert cfg.identity.ir_at == 200
    assert cfg.identity.net == 0  # untouched fields keep their default


# --- Huawei SmartLogger source ----------------------------------------------


def test_shipped_registers_load_both_huawei_sources():
    registers = load_registers("config/registers.json")
    plant = registers.huawei_plant_source
    meter = registers.huawei_meter_source

    # plant: SmartLogger's own aggregated registers, one block on logic device 0
    assert [(g.base, g.count) for g in plant.read_groups] == [(40521, 57)]
    assert plant.points["p_total"].addr == 40525
    assert plant.points["p_total"].dtype == "i32"
    assert plant.points["p_total"].scale == 1.0  # gain 1000 kW -> SI watts
    assert plant.points["pf_total"].dtype == "i16"
    assert plant.points["pf_total"].scale == pytest.approx(0.001)
    assert plant.points["u_l12"].scale == pytest.approx(0.1)

    # meter: table 2-5, two blocks, addressed by the meter's RS485 address
    assert [(g.base, g.count) for g in meter.read_groups] == [(32260, 30), (32335, 30)]
    assert meter.points["imp_energy_total"].dtype == "i64"
    assert meter.points["imp_energy_total"].scale == pytest.approx(0.01)


def test_register_map_huawei_sections_are_optional():
    """Every shipped config_debug_*.json predates this source and must still load."""
    minimal = load_registers("config/registers.json").model_dump(by_alias=True)
    del minimal["huawei_plant_source"]
    del minimal["huawei_meter_source"]
    stripped = RegisterMap.model_validate(minimal)
    assert stripped.huawei_plant_source is None
    assert stripped.huawei_meter_source is None


def test_source_point_rejects_compose_with_an_integer_dtype():
    with pytest.raises(ValidationError, match="only supported for dtype 'float32'"):
        SourcePoint.model_validate({"compose": [100, 102], "dtype": "i32"})


def test_source_point_width_follows_dtype():
    assert SourcePoint(addr=1).width == 2  # float32 default
    assert SourcePoint(addr=1, dtype="u16").width == 1
    assert SourcePoint(addr=1, dtype="i64").width == 4


def test_read_group_count_is_capped_at_one_modbus_response():
    with pytest.raises(ValidationError):
        ReadGroup(base=100, count=MAX_READ_REGISTERS + 1)
    assert ReadGroup(base=100, count=MAX_READ_REGISTERS).count == 125


def test_source_side_rejects_a_point_outside_its_read_groups():
    """Load-time failure beats a permanent fail-safe that looks like an outage."""
    with pytest.raises(ValidationError, match="not fully covered by read_groups"):
        SourceSide.model_validate(
            {
                "read_groups": [{"base": 100, "count": 4}],
                "points": {"x": {"addr": 200, "dtype": "u16"}},
            }
        )


def test_source_side_rejects_a_point_straddling_the_end_of_a_block():
    """A 4-register I64 at the last address would read past the block."""
    with pytest.raises(ValidationError, match="not fully covered by read_groups"):
        SourceSide.model_validate(
            {
                "read_groups": [{"base": 100, "count": 2}],
                "points": {"x": {"addr": 100, "dtype": "i64"}},
            }
        )


def test_source_side_without_read_groups_skips_coverage_checking():
    """The ND45 source keeps using nd45_poller.READ_GROUPS."""
    side = SourceSide.model_validate({"points": {"x": {"addr": 50}}})
    assert side.read_groups is None
    assert side.derive == []


@pytest.mark.parametrize(
    "op,message",
    [
        ({"op": "constant", "targets": ["a"]}, "requires a finite 'value'"),
        ({"op": "constant", "targets": ["a"], "value": 1.0, "from": ["b"]}, "takes no 'from'"),
        ({"op": "copy", "targets": ["a"], "from": ["b", "c"]}, "exactly one 'from'"),
        (
            {"op": "phase_from_line", "targets": ["a", "b"], "from": ["c"]},
            "one 'from' per target",
        ),
        ({"op": "hypot", "targets": ["a"], "from": ["b"]}, "two 'from' points per target"),
        (
            {"op": "ratio_split", "targets": ["a", "b"], "from": ["c"], "weights": ["d"]},
            "one 'weights' entry per target",
        ),
        (
            {"op": "hypot", "targets": ["a"], "from": ["b", "c"], "weights": ["d"]},
            "does not take 'weights'",
        ),
        (
            {"op": "copy", "targets": ["a"], "from": ["b"], "value": 1.0},
            "does not take 'value'",
        ),
    ],
)
def test_derive_op_arity_is_validated(op, message):
    with pytest.raises(ValidationError, match=message):
        DeriveOp.model_validate(op)


def test_derive_op_accepts_the_shipped_plant_and_meter_rules():
    registers = load_registers("config/registers.json")
    plant_ops = [op.op for op in registers.huawei_plant_source.derive]
    meter_ops = [op.op for op in registers.huawei_meter_source.derive]
    # frequency is absent from the whole SmartLogger manual -> a constant
    assert "constant" in plant_ops and "constant" in meter_ops
    assert "phase_from_line" in plant_ops  # plant publishes line voltages only
    assert "ratio_split" in meter_ops  # meter has per-phase P but not per-phase Q


# --- multi-bridge configuration ---------------------------------------------


def _with_bridges(*bridges) -> dict:
    """The shipped nd45 bridge plus the given extras (dropping shipped siblings)."""
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"] = [raw["bridges"][0], *bridges]
    return raw


LEGACY_RAW = {
    "nd45": {"host": "192.168.22.109", "poll_interval_s": 0.3},
    "dtsu": {"transport": "rtu", "slave_id": 10, "rtu": {"port": "/dev/ttyAMA2"}},
    "safety": {"max_data_age_s": 3.0},
}


SECOND_BRIDGE = {
    "name": "smartlogger",
    "source": {"type": "huawei", "host": "10.0.0.5"},
    "dtsu": {"transport": "rtu", "slave_id": 10, "rtu": {"port": "/dev/ttyAMA3"}},
    "safety": {"max_data_age_s": 30.0},
}


def test_every_shipped_bridge_has_the_same_structure():
    """The point of the uniform layout: nd45 and smartlogger differ only in values."""
    raw = json.load(open("config/config.json", encoding="utf-8"))
    assert set(raw) == {"bridges", "prometheus", "static_debug"}
    shapes = [
        (tuple(b), tuple(b["source"]), tuple(b["dtsu"]), tuple(b["safety"]))
        for b in raw["bridges"]
    ]
    assert len(set(shapes)) == 1, "bridge entries must share one key layout"
    assert tuple(raw["bridges"][0]) == (
        "name", "enabled", "source", "dtsu", "safety", "metrics_port"
    )


def test_legacy_keys_become_the_first_bridge():
    """Config files written before multi-bridge support must still load untouched."""
    config = AppConfig.model_validate(LEGACY_RAW)
    specs = config.bridge_specs
    assert [s.name for s in specs] == ["nd45"]
    assert specs[0].source.type == "nd45"
    assert specs[0].source.host == config.nd45.host
    assert specs[0].source.poll_interval_s == config.nd45.poll_interval_s
    assert specs[0].dtsu is config.dtsu
    assert specs[0].safety is config.safety


def test_legacy_keys_may_be_mixed_with_a_bridges_entry():
    config = AppConfig.model_validate({**LEGACY_RAW, "bridges": [SECOND_BRIDGE]})
    assert [b.name for b in config.bridge_specs] == ["nd45", "smartlogger"]


def test_a_config_with_no_bridges_key_at_all_still_loads():
    bare = AppConfig.model_validate(
        {"nd45": {"host": "1.2.3.4"}, "dtsu": {"rtu": {"port": "/dev/null"}}}
    )
    assert bare.bridges == []
    assert len(bare.bridge_specs) == 1


def test_a_config_with_neither_bridges_nor_legacy_keys_is_rejected():
    with pytest.raises(ValidationError, match="defines no bridges"):
        AppConfig.model_validate({"prometheus": {"enabled": False}})


def test_half_the_legacy_pair_is_rejected():
    with pytest.raises(ValidationError, match="must be set together"):
        AppConfig.model_validate({"nd45": {"host": "1.2.3.4"}})


def test_a_config_whose_every_bridge_is_disabled_is_rejected():
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    for bridge in raw["bridges"]:
        bridge["enabled"] = False
    with pytest.raises(ValidationError, match="every configured bridge is disabled"):
        AppConfig.model_validate(raw)


def test_disabled_bridges_are_left_out_of_bridge_specs():
    """The shipped smartlogger entry is a disabled template."""
    config = load_config("config/config.json")
    assert [b.name for b in config.bridges] == ["nd45", "smartlogger"]
    assert config.bridges[1].enabled is False
    assert [b.name for b in config.bridge_specs] == ["nd45"]


def test_an_enabled_bridge_appears_in_bridge_specs():
    config = AppConfig.model_validate(_with_bridges(SECOND_BRIDGE))
    assert [b.name for b in config.bridge_specs] == ["nd45", "smartlogger"]


def test_a_disabled_bridge_may_ship_with_an_empty_host():
    """So the entry can be pre-wired and turned on once the address is known."""
    template = {**SECOND_BRIDGE, "enabled": False,
                "source": {"type": "huawei", "host": ""}}
    config = AppConfig.model_validate(_with_bridges(template))
    assert len(config.bridge_specs) == 1


def test_an_enabled_bridge_with_an_empty_host_is_rejected():
    enabled = {**SECOND_BRIDGE, "source": {"type": "huawei", "host": "   "}}
    with pytest.raises(ValidationError, match="enabled but source.host is empty"):
        AppConfig.model_validate(_with_bridges(enabled))


def test_two_bridges_may_not_share_a_serial_port():
    """The most dangerous misconfiguration -- pymodbus hides the bind failure."""
    clash = {**SECOND_BRIDGE,
             "dtsu": {"transport": "rtu", "slave_id": 10, "rtu": {"port": "/dev/ttyAMA2"}}}
    with pytest.raises(ValidationError, match="both serve RTU on /dev/ttyAMA2"):
        AppConfig.model_validate(_with_bridges(clash))


def test_two_bridges_may_not_share_a_tcp_bind():
    raw = _with_bridges(
        {**SECOND_BRIDGE,
         "dtsu": {"transport": "tcp", "slave_id": 11,
                  "tcp": {"host": "0.0.0.0", "port": 5020}}},
        {**SECOND_BRIDGE, "name": "third",
         "dtsu": {"transport": "tcp", "slave_id": 12,
                  "tcp": {"host": "0.0.0.0", "port": 5020}}},
    )
    with pytest.raises(ValidationError, match="both listen on 0.0.0.0:5020"):
        AppConfig.model_validate(raw)


def test_two_bridges_may_share_a_slave_id_on_separate_buses():
    """Electrically independent RS-485 buses, so slave 10 twice is fine."""
    config = AppConfig.model_validate(_with_bridges(SECOND_BRIDGE))
    ids = [b.dtsu.slave_id for b in config.bridge_specs]
    assert ids == [10, 10]


def test_bridge_names_must_be_unique():
    raw = _with_bridges(SECOND_BRIDGE, {**SECOND_BRIDGE, "dtsu": {
        "transport": "rtu", "slave_id": 10, "rtu": {"port": "/dev/ttyAMA4"}}})
    with pytest.raises(ValidationError, match="duplicate bridge name"):
        AppConfig.model_validate(raw)


def test_the_primary_bridge_name_is_reserved_only_by_the_legacy_keys():
    """With legacy keys present, 'nd45' names *their* bridge and cannot be reused."""
    raw = {**LEGACY_RAW, "bridges": [{**SECOND_BRIDGE, "name": "nd45"}]}
    with pytest.raises(ValidationError, match="reserved"):
        AppConfig.model_validate(raw)
    # ...but in the uniform shape it is just an ordinary bridge name.
    assert load_config("config/config.json").bridge_specs[0].name == "nd45"


def test_a_metrics_port_colliding_with_any_bridge_is_rejected():
    raw = _with_bridges(
        {**SECOND_BRIDGE,
         "dtsu": {"transport": "tcp", "slave_id": 11,
                  "tcp": {"host": "0.0.0.0", "port": 9090}}}
    )
    with pytest.raises(ValidationError, match="collides with bridge 'smartlogger'"):
        AppConfig.model_validate(raw)


def test_a_freshness_threshold_below_two_poll_intervals_is_rejected():
    """Otherwise the bridge sits in permanent fail-safe and looks like a dead source.

    The trap this catches: reusing the ND45's 3.0s on a source polled every 5s.
    """
    raw = _with_bridges({**SECOND_BRIDGE, "safety": {"max_data_age_s": 3.0}})
    with pytest.raises(ValidationError, match="at least twice"):
        AppConfig.model_validate(raw)


def test_the_shipped_thresholds_leave_room_for_a_poll_cycle():
    config = AppConfig.model_validate(_with_bridges(SECOND_BRIDGE))
    for spec in config.bridge_specs:
        assert spec.safety.max_data_age_s >= spec.source.poll_interval_s * 2


def test_a_stall_timeout_at_the_poll_interval_is_rejected():
    """It would fire mid-cycle and rebuild the client of a healthy source forever."""
    with pytest.raises(ValidationError, match="must exceed twice"):
        HuaweiSourceConf(
            host="10.0.0.5", type="huawei", poll_interval_s=5.0, stall_timeout_s=8.0
        )


def test_source_defaults_reflect_each_device_s_own_timing():
    nd45 = Nd45SourceConf(host="1.2.3.4")
    huawei = HuaweiSourceConf(host="1.2.3.4", type="huawei")
    assert (nd45.poll_interval_s, nd45.timeout_s) == (0.3, 1.0)
    # the SmartLogger manual allows a 5s Modbus timeout (section 4.2.4)
    assert (huawei.poll_interval_s, huawei.timeout_s) == (5.0, 6.0)
    assert nd45.register_map == "nd45_source"
    assert huawei.register_map == "huawei_plant_source"
    assert huawei.unit_id == 0  # the SmartLogger's own aggregated registers


def test_shipped_smartlogger_bridge_is_wired_for_the_second_rs485_port():
    config = load_config("config/config.json")
    nd45, bridge = config.bridges
    assert bridge.dtsu.rtu.port == "/dev/ttyAMA3"
    assert bridge.dtsu.rtu.port != nd45.dtsu.rtu.port
    assert bridge.source.register_map == "huawei_plant_source"
    assert bridge.safety.max_data_age_s == 30.0


def test_two_bridges_may_not_share_a_metrics_port():
    """Run as separate services they would race for the bind; the loser retries
    silently, so one service would simply have no metrics endpoint."""
    raw = _with_bridges(  # the shipped nd45 bridge already claims 9090
        {**SECOND_BRIDGE, "metrics_port": 9090},
    )
    with pytest.raises(ValidationError, match="both use metrics_port 9090"):
        AppConfig.model_validate(raw)


def test_a_metrics_port_may_not_collide_with_a_bridge_tcp_output():
    raw = _with_bridges(
        {**SECOND_BRIDGE, "metrics_port": 5020,
         "dtsu": {"transport": "tcp", "slave_id": 11,
                  "tcp": {"host": "0.0.0.0", "port": 5020}}}
    )
    with pytest.raises(ValidationError, match="collides with bridge"):
        AppConfig.model_validate(raw)


def test_metrics_port_falls_back_to_the_shared_one_when_unset():
    config = AppConfig.model_validate(_with_bridges(SECOND_BRIDGE))
    assert config.metrics_port_for("smartlogger") == config.prometheus.port


def test_shipped_config_gives_each_service_its_own_metrics_port():
    """The two systemd instances must not fight over 9090."""
    raw = load_config("config/config.json").model_dump(mode="json", by_alias=True)
    raw["bridges"][1]["enabled"] = True
    raw["bridges"][1]["source"]["host"] = "192.168.22.120"
    config = AppConfig.model_validate(raw)
    ports = {s.name: config.metrics_port_for(s.name) for s in config.bridge_specs}
    assert ports == {"nd45": 9090, "smartlogger": 9091}
    assert len(set(ports.values())) == len(ports)
