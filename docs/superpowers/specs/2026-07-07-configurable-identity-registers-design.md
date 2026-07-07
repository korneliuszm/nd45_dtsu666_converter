# Design: configurable DTSU666 identity/config registers

Date: 2026-07-07

## Problem

The DTSU666 identity/config register block (0x0000-0x002E) is served with values
hardcoded in `_STATIC_INT16_REGISTERS` in `dtsu_server.py`. Two of these
(`net`, current/voltage transformer ratio `IrAt`/`UrAt`) depend on the physical
meter and its installation (CT/PT wiring), and the rest (firmware/programming
code, protocol byte, etc.) are meter-identity values a real DTSU666 reports.
To make the bridge match the identity of a specific real meter (or a
specific installation's CT/PT ratios), these values need to be editable in
`config/config.json` by hand, without touching code.

`bAud` (0x002D) and `Addr` (0x002E) are already config-driven (from
`dtsu.rtu.baudrate` and `dtsu.slave_id` respectively) and are out of scope.

## Approach

Add a new nested Pydantic model `DtsuIdentityConf` in `config.py`, one field
per register, defaulting to the current hardcoded values so an existing
`config.json` without this section behaves exactly as today:

```python
class DtsuIdentityConf(BaseModel):
    rev: int = 100
    ucode: int = 0
    clr_e: int = 0
    net: int = 0
    ir_at: int = 10
    ur_at: int = 10
    disp: int = 0
    b_lcd: int = 0
    endian: int = 0
    protocol: int = 0
```

Add `identity: DtsuIdentityConf = DtsuIdentityConf()` to `DtsuConf`. Example
`config.json` fragment (fields omitted keep their default):

```json
"dtsu": {
  "transport": "rtu",
  "slave_id": 10,
  "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10},
  "rtu": {"port": "/dev/ttyAMA2", "baudrate": 9600, "parity": "N", "stopbits": 1}
}
```

`write_static_registers` in `dtsu_server.py` stops reading the module-level
`_STATIC_INT16_REGISTERS` dict of values and instead builds the
address -> value mapping from `dtsu_cfg.identity`. The register **addresses**
(0x0000, 0x0001, ... 0x002C) stay a hardcoded mapping in code, keyed by
field name -- they're fixed by the DTSU666 protocol, not something to edit.
`bAud`/`Addr` writes are unchanged.

`write_static_registers` already requires `dtsu_cfg: DtsuConf` (it reads
`dtsu_cfg.rtu.baudrate`/`transport` today), so no call-site signature changes
are needed in `build_context`/`app.py`.

## Testing

- `test_config.py`: `DtsuIdentityConf` defaults match current hardcoded
  values; a `DtsuConf` built with a custom `identity` round-trips those
  values.
- `test_server.py`: `build_context` with a `DtsuConf` carrying a custom
  `identity` (e.g. `ir_at=200`) writes that value to the corresponding
  register (0x0006), analogous to the existing `bAud`/`baudrate` test.
- `test_config.json` / README: no required changes since the field is
  optional with defaults; README's "Identity/config registers" section
  (checklist item 8) gets a short note that these are now editable via
  `dtsu.identity` in config instead of `_STATIC_INT16_REGISTERS` in code.

## Out of scope

- No change to `bAud`/`Addr` handling.
- No change to the float32 measurement register map (`registers.json`).
- No validation of register values beyond Pydantic's `int` type check (e.g.
  not range-checking `net` to {0,1}) -- matches the project's existing
  light-validation style for config.
