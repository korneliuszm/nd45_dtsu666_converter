from nd45_dtsu666.config import load_registers
from nd45_dtsu666.diagnostics import render_table


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
