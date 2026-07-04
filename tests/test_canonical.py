import math

from nd45_dtsu666.canonical import CanonicalStore, HealthGate


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
