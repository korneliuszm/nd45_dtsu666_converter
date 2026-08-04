"""Generate docs/smartlogger-dtsu-map.md from config/registers.json.

The mapping tables are derived from the shipped register map rather than typed by
hand, so the document cannot drift from what the bridge actually does. Re-run
after editing the huawei_* sections:

    python scripts/gen_smartlogger_map_doc.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from nd45_dtsu666.config import load_registers  # noqa: E402

DOC = pathlib.Path("docs/smartlogger-dtsu-map.md")
MARK_BEGIN = "<!-- BEGIN GENERATED: {name} -->"
MARK_END = "<!-- END GENERATED: {name} -->"

# Signal names exactly as printed in "SmartLogger ModBus Interface Definitions",
# Issue 35 (2020-02-20): Table 2-1 (SmartLogger) and Table 2-5 (Power Meter).
HUAWEI_DOC = {
    # plant, Table 2-1, logic device 0
    40521: ("Input power", "kW", 1000),
    40525: ("Active power", "kW", 1000),
    40532: ("Power factor", "-", 1000),
    40544: ("Reactive power", "kVar", 1000),
    40560: ("E-Total", "kWh", 10),
    40562: ("E-Daily", "kWh", 10),
    40572: ("Phase A current", "A", 1),
    40573: ("Phase B current", "A", 1),
    40574: ("Phase C current", "A", 1),
    40575: ("Uab", "V", 10),
    40576: ("Ubc", "V", 10),
    40577: ("Uca", "V", 10),
    # power meter, Table 2-5, logic device = its RS485 address
    32260: ("Phase A voltage", "V", 100),
    32262: ("Phase B voltage", "V", 100),
    32264: ("Phase C voltage", "V", 100),
    32266: ("A-B line voltage", "V", 100),
    32268: ("B-C line voltage", "V", 100),
    32270: ("C-A line voltage", "V", 100),
    32272: ("Phase A current", "A", 10),
    32274: ("Phase B current", "A", 10),
    32276: ("Phase C current", "A", 10),
    32278: ("Active power", "kW", 1000),
    32280: ("Reactive power", "kVar", 1000),
    32284: ("Power factor", "-", 1000),
    32287: ("Apparent power", "kVA", 1000),
    32335: ("Phase A active power", "kW", 1000),
    32337: ("Phase B active power", "kW", 1000),
    32339: ("Phase C active power", "kW", 1000),
    32349: ("Negative active electricity", "kWh", 100),
    32353: ("Negative reactive electricity", "kvarh", 100),
    32357: ("Positive active electricity", "kWh", 100),
    32361: ("Positive reactive electricity", "kvarh", 100),
}

SI_UNIT = {
    "u": "V", "i": "A", "p": "W", "q": "var", "s": "VA", "pf": "-",
    "freq": "Hz", "e_daily": "kWh", "dc_power": "W",
}


def si_unit(name: str) -> str:
    if "energy" in name:
        return "kvarh" if "reactive" in name else "kWh"
    if name in SI_UNIT:
        return SI_UNIT[name]
    return SI_UNIT.get(name.split("_")[0], "-")


def num(value: float) -> str:
    """Format a scale without a trailing .0 and without float noise."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def source_table(side) -> str:
    rows = [
        "| Rejestr | Hex | Sygnał wg dokumentacji Huawei | Typ | Gain | Jedn. Huawei "
        "| → punkt kanoniczny | `scale` | `sign` | Jedn. SI |",
        "|---:|---|---|---|---:|---|---|---:|---:|---|",
    ]
    for name, pt in sorted(side.points.items(), key=lambda kv: kv[1].addr):
        doc_name, unit, gain = HUAWEI_DOC.get(pt.addr, ("?", "?", "?"))
        # sign is a column of its own: -1 means the served value is the negative
        # of what Huawei reports, which is too easy to miss folded into `scale`.
        sign = "**−1**" if pt.sign < 0 else "+1"
        rows.append(
            f"| {pt.addr} | 0x{pt.addr:04X} | {doc_name} | {pt.dtype.upper()} | {gain} "
            f"| {unit} | `{name}` | ×{num(pt.scale)} | {sign} | {si_unit(name)} |"
        )
    return "\n".join(rows)


def read_groups_table(side, unit_label: str) -> str:
    rows = [
        "| Blok | Rejestr początkowy | Liczba rejestrów | Zakres | logic device ID |",
        "|---:|---:|---:|---|---|",
    ]
    for index, group in enumerate(side.read_groups or [], start=1):
        end = group.base + group.count - 1
        rows.append(
            f"| {index} | {group.base} | {group.count} | {group.base}–{end} | {unit_label} |"
        )
    return "\n".join(rows)


_OP_FORMULA = {
    "constant": lambda op: f"= {num(op.value)} (stała)",
    "copy": lambda op: f"= `{op.from_[0]}`",
    "split_equal": lambda op: f"= `{op.from_[0]}` / {len(op.targets)}",
    "phase_from_line": lambda op: "= napięcie międzyfazowe / √3",
    "hypot": lambda op: "= √(a² + b²)",
    # pipes escaped: an unescaped one would split the markdown table cell
    "ratio_split": lambda op: (
        rf"= `{op.from_[0]}` × \|waga\| / Σ\|wagi\|; przy Σ≈0 podział równy"
    ),
    "pf_from_p_s": lambda op: "= P / S; przy S=0 → 1.0",
}


def derive_table(side) -> str:
    rows = [
        "| Punkt(y) kanoniczne | Operacja | Źródła | Wzór |",
        "|---|---|---|---|",
    ]
    for op in side.derive:
        targets = ", ".join(f"`{t}`" for t in op.targets)
        if op.op == "ratio_split":
            sources = ", ".join(f"`{s}`" for s in [*op.from_, *op.weights])
        elif op.from_:
            sources = ", ".join(f"`{s}`" for s in op.from_)
        else:
            sources = "–"
        rows.append(
            f"| {targets} | `{op.op}` | {sources} | {_OP_FORMULA[op.op](op)} |"
        )
    return "\n".join(rows)


def target_table(side, with_units: bool = True) -> str:
    header = "| Adres | Hex | `from` (punkt kanoniczny) | `scale` | /CT | Uwagi |"
    rows = [header, "|---:|---|---|---:|:--:|---|"]
    for _name, pt in sorted(side.points.items(), key=lambda kv: kv[1].addr):
        notes = []
        if pt.zero_low_word:
            notes.append("tylko starsze słowo IEEE754")
        if pt.sign != 1:
            notes.append(f"sign {pt.sign:+d}")
        if with_units:
            notes.append(si_unit(pt.from_))
        rows.append(
            f"| {pt.addr} | 0x{pt.addr:04X} | `{pt.from_}` | ×{num(pt.scale)} "
            f"| {'✓' if pt.divide_by_ct else '–'} | {', '.join(notes) or '–'} |"
        )
    return "\n".join(rows)


def coverage_table(registers) -> str:
    plant = registers.huawei_plant_source
    meter = registers.huawei_meter_source
    required = sorted(
        {pt.from_ for _n, side in registers.targets for pt in side.points.values()}
    )
    derived_by = {"active_energy_total", "net_imp_energy_total", "net_exp_energy_total"}
    rows = [
        "| Punkt kanoniczny | Jedn. | `huawei_plant_source` | `huawei_meter_source` |",
        "|---|---|---|---|",
    ]
    for name in required:
        cells = []
        for side in (plant, meter):
            if name in side.points:
                cells.append(f"rejestr {side.points[name].addr}")
            elif any(name in op.targets for op in side.derive):
                op = next(op for op in side.derive if name in op.targets)
                cells.append(f"`{op.op}`")
            elif name in derived_by:
                cells.append("`compute_derived`")
            else:
                cells.append("**BRAK**")
        rows.append(f"| `{name}` | {si_unit(name)} | {cells[0]} | {cells[1]} |")
    return "\n".join(rows)


def splice(text: str, name: str, body: str) -> str:
    begin, end = MARK_BEGIN.format(name=name), MARK_END.format(name=name)
    if begin not in text or end not in text:
        raise SystemExit(f"marker for {name!r} missing from {DOC}")
    head = text[: text.index(begin) + len(begin)]
    tail = text[text.index(end):]
    return f"{head}\n\n{body}\n\n{tail}"


def main() -> int:
    registers = load_registers("config/registers.json")
    plant, meter = registers.huawei_plant_source, registers.huawei_meter_source
    text = DOC.read_text(encoding="utf-8")
    sections = {
        "plant-blocks": read_groups_table(plant, "0 (SmartLogger)"),
        "plant-source": source_table(plant),
        "plant-derive": derive_table(plant),
        "meter-blocks": read_groups_table(meter, "adres RS485 licznika"),
        "meter-source": source_table(meter),
        "meter-derive": derive_table(meter),
        "coverage": coverage_table(registers),
        "fc03-measurements": target_table(registers.dtsu_target),
        "fc04-measurements": target_table(registers.dtsu_sigen_ext_target),
        "fc04-energy": target_table(registers.dtsu_sigen_ext_energy),
    }
    for name, body in sections.items():
        text = splice(text, name, body)
    DOC.write_text(text, encoding="utf-8")
    print(f"regenerated {len(sections)} tables in {DOC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
