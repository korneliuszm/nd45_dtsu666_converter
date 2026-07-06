import pytest
from pydantic import ValidationError

from nd45_dtsu666.config import DtsuConf, DtsuRtuConf, DtsuTcpConf, load_config, load_registers


def test_load_registers_reads_seed(tmp_path):
    reg = load_registers("config/registers.json")
    assert reg.nd45_source.points["u_l1"].addr == 50
    assert reg.dtsu_target.points["u_l1"].addr == 8198
    assert reg.dtsu_target.points["u_l1"].from_ == "u_l1"
    assert reg.dtsu_target.points["u_l1"].scale == 10
    # energy is a two-register compose on the ND45 side
    assert reg.nd45_source.points["imp_energy_total"].compose == [912, 914]
    assert reg.nd45_source.points["imp_energy_total"].factors == [1000, 1]


def test_load_config_reads_seed():
    cfg = load_config("config/config.json")
    assert cfg.nd45.port == 502
    assert cfg.dtsu.slave_id == 1
    assert cfg.dtsu.transport == "rtu"
    assert cfg.dtsu.rtu.port == "/dev/ttyAMA2"
    assert cfg.dtsu.tcp.port == 502
    assert cfg.safety.max_data_age_s == 3.0


def test_target_point_defaults(tmp_path):
    reg = load_registers("config/registers.json")
    pf = reg.dtsu_target.points["pf_total"]
    assert pf.sign == 1
    assert pf.offset == 0


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
