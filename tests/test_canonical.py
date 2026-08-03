import math

import pytest

from nd45_dtsu666.canonical import (
    CanonicalStore,
    HealthGate,
    apply_derive,
    compute_derived,
)
from nd45_dtsu666.config import DeriveOp


def test_new_store_is_infinitely_old():
    store = CanonicalStore()
    assert store.age(now=100.0) == math.inf
    assert store.is_fresh(now=100.0, max_age=3.0) is False


def test_update_then_snapshot():
    store = CanonicalStore()
    store.update({"p_total": 1500.0}, ts=10.0)
    values, ts = store.snapshot()
    assert values == {"p_total": 1500.0}
    assert ts == 10.0


def test_snapshot_is_a_copy():
    store = CanonicalStore()
    store.update({"p_total": 1.0}, ts=1.0)
    values, _ = store.snapshot()
    values["p_total"] = 999.0
    again, _ = store.snapshot()
    assert again["p_total"] == 1.0


def test_age_and_freshness():
    store = CanonicalStore()
    store.update({"x": 1.0}, ts=100.0)
    assert store.age(now=102.0) == 2.0
    assert store.is_fresh(now=102.0, max_age=3.0) is True
    assert store.is_fresh(now=104.0, max_age=3.0) is False


def test_health_gate():
    gate = HealthGate(max_age=3.0)
    assert gate.should_serve(age=1.0) is True
    assert gate.should_serve(age=3.0) is True
    assert gate.should_serve(age=3.1) is False


# --- compute_derived ---------------------------------------------------------


def test_compute_derived_fills_the_energy_aliases():
    values = {"imp_energy_total": 7.0, "exp_energy_total": 3.0}
    compute_derived(values)
    assert values["active_energy_total"] == pytest.approx(10.0)
    assert values["net_imp_energy_total"] == pytest.approx(7.0)
    assert values["net_exp_energy_total"] == pytest.approx(3.0)


# --- apply_derive ------------------------------------------------------------


def _op(**kwargs):
    return DeriveOp.model_validate(kwargs)


def test_derive_constant():
    values: dict[str, float] = {}
    apply_derive(values, [_op(op="constant", targets=["freq", "other"], value=50.0)])
    assert values == {"freq": 50.0, "other": 50.0}


def test_derive_copy():
    values = {"pf_total": -0.95}
    apply_derive(values, [_op(op="copy", targets=["pf_l1", "pf_l2"], **{"from": ["pf_total"]})])
    assert values["pf_l1"] == values["pf_l2"] == pytest.approx(-0.95)


def test_derive_split_equal():
    values = {"p_total": 900.0}
    apply_derive(
        values,
        [_op(op="split_equal", targets=["p_l1", "p_l2", "p_l3"], **{"from": ["p_total"]})],
    )
    assert values["p_l1"] == values["p_l2"] == values["p_l3"] == pytest.approx(300.0)


def test_derive_phase_from_line():
    values = {"u_l12": 400.0, "u_l23": 401.0, "u_l31": 402.0}
    apply_derive(
        values,
        [
            _op(
                op="phase_from_line",
                targets=["u_l1", "u_l2", "u_l3"],
                **{"from": ["u_l12", "u_l23", "u_l31"]},
            )
        ],
    )
    assert values["u_l1"] == pytest.approx(400.0 / math.sqrt(3.0))
    assert values["u_l3"] == pytest.approx(402.0 / math.sqrt(3.0))


def test_derive_hypot_pairs_two_sources_per_target():
    values = {"p_l1": 3.0, "q_l1": 4.0, "p_l2": 6.0, "q_l2": 8.0}
    apply_derive(
        values,
        [
            _op(
                op="hypot",
                targets=["s_l1", "s_l2"],
                **{"from": ["p_l1", "q_l1", "p_l2", "q_l2"]},
            )
        ],
    )
    assert values["s_l1"] == pytest.approx(5.0)
    assert values["s_l2"] == pytest.approx(10.0)


def test_derive_ratio_split_apportions_by_weight():
    values = {"q_total": 6000.0, "p_l1": -10000.0, "p_l2": -20000.0, "p_l3": -30000.0}
    apply_derive(
        values,
        [
            _op(
                op="ratio_split",
                targets=["q_l1", "q_l2", "q_l3"],
                weights=["p_l1", "p_l2", "p_l3"],
                **{"from": ["q_total"]},
            )
        ],
    )
    # weights are taken as magnitudes, so an exporting (negative) P still splits
    assert values["q_l1"] == pytest.approx(1000.0)
    assert values["q_l2"] == pytest.approx(2000.0)
    assert values["q_l3"] == pytest.approx(3000.0)
    assert values["q_l1"] + values["q_l2"] + values["q_l3"] == pytest.approx(6000.0)


def test_derive_ratio_split_falls_back_to_equal_on_zero_weights():
    values = {"q_total": 900.0, "p_l1": 0.0, "p_l2": 0.0, "p_l3": 0.0}
    apply_derive(
        values,
        [
            _op(
                op="ratio_split",
                targets=["q_l1", "q_l2", "q_l3"],
                weights=["p_l1", "p_l2", "p_l3"],
                **{"from": ["q_total"]},
            )
        ],
    )
    assert values["q_l1"] == values["q_l2"] == values["q_l3"] == pytest.approx(300.0)


def test_derive_pf_from_p_s_is_unity_at_zero_apparent_power():
    values = {"p_l1": 0.0, "s_l1": 0.0, "p_l2": -9.9, "s_l2": 10.0}
    apply_derive(
        values,
        [
            _op(
                op="pf_from_p_s",
                targets=["pf_l1", "pf_l2"],
                **{"from": ["p_l1", "s_l1", "p_l2", "s_l2"]},
            )
        ],
    )
    assert values["pf_l1"] == pytest.approx(1.0)
    assert values["pf_l2"] == pytest.approx(-0.99)


def test_derive_missing_input_yields_nan_rather_than_a_made_up_number():
    values: dict[str, float] = {}
    apply_derive(values, [_op(op="copy", targets=["pf_l1"], **{"from": ["absent"]})])
    assert math.isnan(values["pf_l1"])


def test_derive_steps_run_in_order_and_can_consume_earlier_results():
    """The plant map relies on this: hypot builds s_total, then split_equal fans it out."""
    values = {"p_total": 3.0, "q_total": 4.0}
    apply_derive(
        values,
        [
            _op(op="hypot", targets=["s_total"], **{"from": ["p_total", "q_total"]}),
            _op(
                op="split_equal",
                targets=["s_l1", "s_l2", "s_l3"],
                **{"from": ["s_total"]},
            ),
        ],
    )
    assert values["s_total"] == pytest.approx(5.0)
    assert values["s_l1"] == pytest.approx(5.0 / 3)
