import pytest

from nd45_dtsu666.config import load_config, load_registers
from nd45_dtsu666.diagnostics import _synthetic_values, render_table, select_bridge


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
        canonical={"active_energy_total": 24690.0},
        age=0.4,
        healthy=True,
        ct_ratio=200.0,
    )

    coarse_line = next(line for line in table.splitlines() if "4096" in line)
    assert coarse_line.split()[-1] == "123.0"


# --- select_bridge (the shared --bridge resolver for diag/selftest/static) ---


def test_select_bridge_defaults_to_the_first_bridge():
    config = load_config("config/config.json")
    assert select_bridge(config, None).name == config.bridge_specs[0].name


def test_select_bridge_returns_the_named_bridge():
    # The second bridge's name is read from the config rather than hardcoded:
    # which source feeds it is a config choice (huawei or etango), and this
    # test is about name resolution, not about which meter is wired up today.
    config = load_config("config/config.json")
    second = config.bridge_specs[1].name
    assert select_bridge(config, second).name == second


def test_select_bridge_rejects_an_unknown_name():
    config = load_config("config/config.json")
    configured = ", ".join(s.name for s in config.bridge_specs)
    with pytest.raises(
        SystemExit, match=f"unknown bridge 'bogus' \\(configured: {configured}\\)"
    ):
        select_bridge(config, "bogus")


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


# --- sample spread (telling a jittery source from a steady one) --------------


def _stats(*samples, points=("p_total",)):
    from nd45_dtsu666.diagnostics import SampleStats

    stats = SampleStats(points)
    for values in samples:
        stats.record(values)
    return stats


def test_sample_stats_tracks_the_spread_of_each_point():
    stats = _stats({"p_total": -100.0}, {"p_total": 40.0}, {"p_total": -20.0})
    text = stats.render(interval=0.3)
    assert "3 sample(s) at 0.3s" in text
    assert "-100.000" in text and "40.000" in text
    assert "140.000" in text  # peak-to-peak


def test_sample_stats_counts_sign_changes():
    """The number that answers "is it really crossing zero, or is it noise?"."""
    stats = _stats(
        {"p_total": 10.0}, {"p_total": -5.0}, {"p_total": 8.0}, {"p_total": 9.0}
    )
    assert stats.render(interval=1.0).split("\n")[-1].split()[-1] == "2"


def test_sample_stats_does_not_count_a_pass_through_zero_twice():
    stats = _stats({"p_total": 5.0}, {"p_total": 0.0}, {"p_total": -5.0})
    assert stats.render(interval=1.0).split("\n")[-1].split()[-1] == "1"


def test_sample_stats_skips_missing_and_non_finite_samples():
    """A failed poll passes {} in; it must not be charted as a value."""
    stats = _stats({"p_total": 7.0}, {}, {"p_total": float("nan")}, {"p_total": 7.0})
    assert stats.samples == 4
    line = stats.render(interval=1.0).split("\n")[-1]
    assert line.split()[1:4] == ["7.000", "7.000", "7.000"]


def test_sample_stats_renders_a_point_that_never_arrived():
    stats = _stats({}, points=("p_total", "freq"))
    assert "freq" in stats.render(interval=1.0)
