# nd45_dtsu666_converter

Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map (Modbus RTU)
so a Sigenergy storage system can read it as a "Power Sensor".

See `docs/superpowers/specs/2026-07-04-nd45-dtsu666-translator-design.md` for the design.

## Quick start
```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```
