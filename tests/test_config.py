import math

import pytest
from pydantic import ValidationError

from nd45_dtsu666.config import (
    AppConfig,
    DtsuConf,
    DtsuIdentityConf,
    DtsuRtuConf,
    DtsuTcpConf,
    Nd45Conf,
    SafetyConf,
    SourcePoint,
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
    assert cfg.nd45.port == 502
    assert cfg.dtsu.slave_id == 10
    assert cfg.dtsu.transport == "rtu"
    assert cfg.dtsu.rtu.port == "/dev/ttyAMA2"
    assert cfg.dtsu.tcp.port == 502
    assert cfg.safety.max_data_age_s == 3.0
    assert cfg.dtsu.identity.rev == 103
    assert cfg.dtsu.identity.ucode == 701
    assert cfg.dtsu.identity.ir_at == 200
    assert cfg.dtsu.identity.ur_at == 10
    assert cfg.static_debug.feed_interval_s == 0.5
    assert cfg.static_debug.values["u_l1"] == 9000.0
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
    cfg = load_config("config/config.json")
    assert cfg.nd45.reconnect_delay_s == 1.0
    assert cfg.nd45.reconnect_delay_max_s == 30.0
    assert cfg.safety.min_restart_interval_s == 5.0


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
