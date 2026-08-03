"""The SmartLogger mapping doc must stay in sync with the register map.

`docs/smartlogger-dtsu-map.md` has its tables generated from
`config/registers.json`. Editing the map without regenerating would leave the
document quietly wrong -- which for a register mapping is worse than having no
document, since it is what someone reaches for during bring-up.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

DOC = pathlib.Path("docs/smartlogger-dtsu-map.md")
GENERATOR = pathlib.Path("scripts/gen_smartlogger_map_doc.py")


def _generator():
    """Import the generator script, which lives outside the package."""
    spec = importlib.util.spec_from_file_location("gen_doc", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_and_doc_are_present():
    assert DOC.exists(), f"{DOC} is missing"
    assert GENERATOR.exists(), f"{GENERATOR} is missing"


def test_every_generated_table_matches_the_shipped_register_map():
    """Regenerating must be a no-op; if this fails, run the generator."""
    from nd45_dtsu666.config import load_registers

    gen = _generator()
    registers = load_registers("config/registers.json")
    plant, meter = registers.huawei_plant_source, registers.huawei_meter_source
    expected = {
        "plant-blocks": gen.read_groups_table(plant, "0 (SmartLogger)"),
        "plant-source": gen.source_table(plant),
        "plant-derive": gen.derive_table(plant),
        "meter-blocks": gen.read_groups_table(meter, "adres RS485 licznika"),
        "meter-source": gen.source_table(meter),
        "meter-derive": gen.derive_table(meter),
        "coverage": gen.coverage_table(registers),
        "fc03-measurements": gen.target_table(registers.dtsu_target),
        "fc04-measurements": gen.target_table(registers.dtsu_sigen_ext_target),
        "fc04-energy": gen.target_table(registers.dtsu_sigen_ext_energy),
    }
    text = DOC.read_text(encoding="utf-8")
    stale = [
        name
        for name, body in expected.items()
        if body not in text
    ]
    assert not stale, (
        "these tables are out of date: "
        + ", ".join(stale)
        + " -- run `python scripts/gen_smartlogger_map_doc.py`"
    )


def test_every_huawei_register_has_a_documented_signal_name():
    """A new point in the map needs its Huawei doc name added to the generator."""
    from nd45_dtsu666.config import load_registers

    gen = _generator()
    registers = load_registers("config/registers.json")
    undocumented = sorted(
        pt.addr
        for side in (registers.huawei_plant_source, registers.huawei_meter_source)
        for pt in side.points.values()
        if pt.addr not in gen.HUAWEI_DOC
    )
    assert not undocumented, (
        f"registers {undocumented} have no Huawei signal name; add them to "
        "HUAWEI_DOC in scripts/gen_smartlogger_map_doc.py"
    )


def test_documented_gains_agree_with_the_scales_in_the_map():
    """The doc claims scale = (1/Gain) x unit factor -- check it actually holds.

    Catches a scale edited in registers.json without rethinking the Gain, which
    would silently mis-scale a served register by a power of ten.
    """
    from nd45_dtsu666.config import load_registers

    gen = _generator()
    registers = load_registers("config/registers.json")
    # Huawei unit -> factor converting it to this project's SI unit
    unit_factor = {"kW": 1000.0, "kVar": 1000.0, "kVA": 1000.0, "V": 1.0,
                   "A": 1.0, "kWh": 1.0, "kvarh": 1.0, "-": 1.0}
    mismatched = []
    for side in (registers.huawei_plant_source, registers.huawei_meter_source):
        for name, pt in side.points.items():
            _doc_name, unit, gain = gen.HUAWEI_DOC[pt.addr]
            expected = unit_factor[unit] / gain
            if pt.scale != pytest.approx(expected):
                mismatched.append((name, pt.addr, pt.scale, expected))
    assert not mismatched, f"scale != (1/Gain) x unit factor for: {mismatched}"


def test_doc_links_resolve():
    """Relative links in the doc must point at files that exist."""
    import re

    text = DOC.read_text(encoding="utf-8")
    targets = re.findall(r"\]\((?!https?://)([^)#]+)", text)
    missing = [t for t in targets if not (DOC.parent / t).exists()]
    assert not missing, f"broken relative links: {missing}"
