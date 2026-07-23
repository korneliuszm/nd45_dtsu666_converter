import asyncio
import logging

import nd45_dtsu666.rtudebug as rtudebug_mod
from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.rtudebug import LoggingRtuActivity, RegisterNameIndex


def _index():
    registers = load_registers("config/registers.json")
    return RegisterNameIndex.build(
        [registers.dtsu_target, registers.dtsu_sigen_ext_target, registers.dtsu_sigen_ext_energy],
        registers.dtsu_sigen_identity,
    )


def test_lookup_single_measurement_point():
    idx = _index()
    # u_l12 lives at 8192 as a float32 (2 words: 8192, 8193).
    assert idx.lookup(8192, 2) == ["u_l12"]


def test_lookup_block_spans_multiple_points():
    idx = _index()
    # A 6-word block from 8192 covers u_l12/u_l23/u_l31 (2 words each).
    names = idx.lookup(8192, 6)
    assert names == ["u_l12", "u_l23", "u_l31"]


def test_lookup_partial_overlap_still_matches():
    idx = _index()
    # Starting mid-point (8193 is the 2nd word of u_l12) must still name u_l12.
    assert "u_l12" in idx.lookup(8193, 1)


def test_lookup_identity_register():
    idx = _index()
    # rev is the single-word identity register at 0x0000.
    assert idx.lookup(0x0000, 1) == ["rev"]


def test_lookup_sigen_fc04_point_is_function_code_aware():
    idx = _index()

    assert idx.lookup(0x151C, 2, function_code=4) == ["p_total"]
    assert idx.lookup(0x151C, 2, function_code=3) == []


def test_lookup_sigen_identity_field():
    idx = _index()

    assert idx.lookup(0xF100, 20, function_code=3) == ["model_string"]
    assert idx.lookup(0xF114, 2, function_code=3) == ["handshake_magic"]


def test_lookup_sigen_ext_energy_points_at_offset_0x800():
    idx = _index()

    # imp_ep sits at the tail end of the 0x180A/qty22 range Sigenergy polls.
    assert idx.lookup(0x180A, 22, function_code=4) == ["imp_ep"]
    # 0x1828/qty4 covers exp_ep and exp_ep_l1.
    assert idx.lookup(0x1828, 4, function_code=4) == ["exp_ep", "exp_ep_l1"]


def test_lookup_unmapped_block_returns_empty():
    idx = _index()
    # A gap between the identity block and the energy/measurement blocks.
    assert idx.lookup(1000, 2) == []


def test_logging_activity_logs_annotated_line_and_counts(caplog):
    idx = _index()
    activity = LoggingRtuActivity(idx)
    with caplog.at_level(logging.INFO, logger="nd45_dtsu666.rtudebug"):
        activity.record(3, 8192, 2, ts=100.0)
    assert any(
        "READ FC03" in r.message and "addr=8192" in r.message
        and "0x2000" in r.message and "u_l12" in r.message
        for r in caplog.records
    )
    # super().record() still ran -> block accounting is intact.
    assert activity.total == 1
    assert activity.summary(100.0)["blocks"] == [((3, 8192, 2), 1)]


def test_logging_activity_marks_unmapped(caplog):
    idx = _index()
    activity = LoggingRtuActivity(idx)
    with caplog.at_level(logging.INFO, logger="nd45_dtsu666.rtudebug"):
        activity.record(3, 1000, 2, ts=1.0)
    assert any("(unmapped)" in r.message for r in caplog.records)


async def test_run_rtudebug_returns_cleanly_when_never_connected(monkeypatch):
    async def _never_connect(client, stop_event, *args, **kwargs):
        return False

    monkeypatch.setattr(rtudebug_mod, "connect_with_retry", _never_connect)
    config = load_config("config/config.json")
    registers = load_registers("config/registers.json")
    await asyncio.wait_for(
        rtudebug_mod.run_rtudebug(config, registers, asyncio.Event()), timeout=1.0
    )
