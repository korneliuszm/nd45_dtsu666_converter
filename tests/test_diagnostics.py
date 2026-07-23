import pytest

from nd45_dtsu666.config import load_registers
from nd45_dtsu666.diagnostics import _synthetic_values, render_table


def test_render_table_contains_key_columns_and_values():
    reg = load_registers("config/registers.json")
    table = render_table(
        reg.nd45_source, reg.dtsu_target,
        canonical={"u_l1": 230.0, "p_total": 1500.0},
        age=0.4, healthy=True,
    )
    assert "u_l1" in table
    assert "230.0" in table
    assert "p_total" in table
    assert "1500.0" in table
    # shows the DTSU target address for u_l1 (8198)
    assert "8198" in table
    assert "age" in table.lower()
    assert "OK" in table or "HEALTHY" in table.upper()


def test_render_table_shows_stale_status():
    reg = load_registers("config/registers.json")
    table = render_table(reg.nd45_source, reg.dtsu_target, canonical={}, age=99.0, healthy=False)
    assert "STALE" in table.upper() or "FAIL" in table.upper()


def test_render_table_matches_ct_divided_coarse_energy_encoding():
    reg = load_registers("config/registers.json")

    table = render_table(
        reg.nd45_source,
        reg.dtsu_target,
        canonical={"imp_energy_total": 24690.0},
        age=0.4,
        healthy=True,
        ct_ratio=200.0,
    )

    coarse_line = next(line for line in table.splitlines() if "4096" in line)
    assert coarse_line.split()[-1] == "123.0"


def test_synthetic_values_cover_every_dtsu_target_source():
    # selftest is the operator's mbpoll bench tool: every register in the DTSU
    # map must serve a value, including the net_* energies that poll_once
    # normally derives -- otherwise the bench shows them stuck at 0 and the
    # installer wrongly concludes those registers are broken.
    reg = load_registers("config/registers.json")
    values = _synthetic_values(reg)
    for key, pt in reg.dtsu_target.points.items():
        assert pt.from_ in values, f"selftest serves nothing for {key} (from {pt.from_})"
    assert values["active_energy_total"] == pytest.approx(1234.5 + 67.8)
    assert values["net_imp_energy_total"] == pytest.approx(1234.5)
    assert values["net_exp_energy_total"] == pytest.approx(67.8)
    assert values["reactive_imp_energy_total"] == 0.0
    assert values["reactive_exp_energy_total"] == 0.0
