# Sigen Energy Register Fidelity Design

## Goal

Make the converter's FC03 and FC04 energy registers match the physical
Sigenergy TPX-CH/DTSU666 meter under both import and export, and expose every
independent simulated measurement through `config/config.json`.

The design is based on the three-phase scans:

- `scan_COM8_20260723_171145.txt`
- `scan_COM8_20260723_175850.txt`
- `scan_COM8_20260723_212641.txt`

The last scan used reversed current direction and therefore provides the
export reference.

## Scope

This change covers all confirmed energy-register discrepancies, including
aliases that Sigenergy did not query in the captured traffic.

It does not add the independently observed phase-angle registers
`0x153C`-`0x1540` or `0x2032`-`0x2036`. Those values are outside this
energy-correction scope.

## Confirmed physical behavior

The reverse-flow scan reports:

- `P total` at `0x151C`: `-3.058557 kW`;
- `ImpEp` at `0x181E`: `7.0078125 kWh`;
- export at `0x1828`: `0.19921875 kWh`;
- export alias at `0x1830`: `0.19921875 kWh`;
- phase-export registers `0x182A`-`0x182E`: zero;
- `0x180A`: `2.796875`, distinct from active import.

Across the supplied scans, `0x180A` tracks accumulated reactive energy rather
than active import. The physical aliases `0x100A`, `0x1050`, and `0x1850`
track the same quantity after the appropriate CT-side conversion.

The physical "net" fields are directional aliases, not arithmetic
import-minus-export values:

- `0x1026` and `0x1826` repeat active import;
- `0x1030` and `0x1830` repeat active export.

The physical coarse aliases always contain a valid float32 high word followed
by a zero low word. This applies to `0x1000`, `0x100A`, `0x1050`, `0x1800`,
`0x180A`, and `0x1850`.

## ND45 source model

The existing energy poll covers ND45 registers `900`-`931`. It will be
expanded to one Modbus block covering registers `900`-`995`, still below the
125-register Modbus limit.

The source map will add the four total reactive quadrant counters:

| Canonical component | ND45 Mvarh | ND45 kvarh |
|---|---:|---:|
| reactive imported inductive | 944 | 946 |
| reactive exported inductive | 960 | 962 |
| reactive imported capacitive | 976 | 978 |
| reactive exported capacitive | 992 | 994 |

Each component is calculated as:

```text
component_kvarh = Mvarh * 1000 + kvarh
```

The canonical total is:

```text
reactive_energy_total =
    reactive_imported_inductive +
    reactive_exported_inductive +
    reactive_imported_capacitive +
    reactive_exported_capacitive
```

This uses the ND45's persistent hardware counters. The converter will not
integrate instantaneous reactive power and therefore requires no local energy
state or persistence.

The active derived values become:

```text
active_energy_total = imp_energy_total + exp_energy_total
net_imp_energy_total = imp_energy_total
net_exp_energy_total = exp_energy_total
```

The existing `net_*` canonical names remain for compatibility with the target
map, but their documented meaning changes from arithmetic net energy to the
directional aliases observed on the physical meter.

## Target register map

### FC04 primary-side Sigen map

All values are primary-side values and are not divided by the configured CT
ratio.

| Address | Output |
|---|---|
| `0x1800` | coarse `active_energy_total` |
| `0x180A` | coarse `reactive_energy_total` |
| `0x181E` | precise `imp_energy_total` |
| `0x1820` | precise `imp_energy_l1` |
| `0x1822` | precise `imp_energy_l2` |
| `0x1824` | precise `imp_energy_l3` |
| `0x1826` | precise copy of `imp_energy_total` |
| `0x1828` | precise `exp_energy_total` |
| `0x182A`-`0x182E` | constant zero |
| `0x1830` | precise copy of `exp_energy_total` |
| `0x1850` | coarse `reactive_energy_total` alias |

### FC03 secondary/CT-side map

Dynamic energy values in this map are divided by
`dtsu.identity.ir_at`.

| Address | Output |
|---|---|
| `0x1000` | coarse `imp_energy_total / CT` |
| `0x100A` | coarse `reactive_energy_total / CT` |
| `0x101E` | precise `imp_energy_total / CT` |
| `0x1020` | precise `imp_energy_l1 / CT` |
| `0x1022` | precise `imp_energy_l2 / CT` |
| `0x1024` | precise `imp_energy_l3 / CT` |
| `0x1026` | precise copy of `imp_energy_total / CT` |
| `0x1028`-`0x102E` | constant zero |
| `0x1030` | precise `exp_energy_total / CT` |
| `0x1050` | coarse `reactive_energy_total / CT` alias |

Removing a dynamic point from the target map leaves its two registers zero in
the preallocated sequential datastore. The constant-zero phase-export ranges
therefore need no independent runtime values.

## Coarse float encoding

`TargetPoint` will gain a boolean option named `zero_low_word`, defaulting to
`false`.

After normal scaling, sign handling, CT division, finite-value validation, and
float32 encoding, a point with `zero_low_word: true` will set the logical low
word of the float32 representation to `0x0000`. The implementation must honor
the configured word order rather than assuming the second list element is
always the logical low word.

Only the six confirmed physical aliases use this option:

- FC03: `0x1000`, `0x100A`, `0x1050`;
- FC04: `0x1800`, `0x180A`, `0x1850`.

Precise energy registers retain all 32 encoded bits.

## Static simulation

The static pipeline and live ND45 pipeline continue to share the same target
map, encoding, CT conversion, and derived-value logic.

`STATIC_DEBUG_VALUE_KEYS` will add `reactive_energy_total`. The example
`static_debug.values` in `config/config.json` will explicitly contain:

- all phase and line voltages;
- all phase currents;
- phase and total active power;
- phase and total reactive power;
- phase and total apparent power;
- phase and total power factor;
- frequency;
- total and per-phase active import energy;
- total active export energy;
- total reactive energy.

The checked-in example will represent reverse flow:

- active powers and power factors are negative;
- reactive powers are positive;
- active export is non-zero.

The following values remain derived and cannot conflict with configured
inputs:

- `active_energy_total`;
- `net_imp_energy_total`;
- `net_exp_energy_total`;
- CT-side aliases;
- coarse aliases.

The confirmed constant-zero export-phase registers are intentionally not
configurable.

## Error handling and atomicity

The ND45 poll treats all eight source float32 values used for the four
reactive quadrant totals as critical:

- a Modbus error rejects the poll;
- a short response rejects the poll;
- `NaN`, infinity, ND45 overrange, or float overflow rejects the poll;
- no invalid reactive component is silently replaced with zero.

The existing freshness gate remains unchanged. Repeated poll failures make
the previous sample stale, after which the DTSU server stops responding.

`update_datastore` continues to encode every point before performing the first
write. A failure in scaling, CT conversion, float32 encoding, or coarse-word
handling therefore preserves the complete previous register image.

Static mode uses the same validation. Invalid configured values fail
synchronously during startup.

## Testing

Implementation follows test-driven development.

### Poller tests

- assert that `READ_GROUPS` covers `900`-`995`;
- decode each reactive quadrant Mvarh/kvarh pair;
- assert that their sum produces `reactive_energy_total`;
- assert that an invalid component rejects the whole poll;
- retain the source-coverage startup validation.

### Derived-value tests

For:

```text
import = 7.0078125
export = 0.19921875
reactive = 2.796875
```

assert:

```text
active_energy_total = 7.20703125
net_imp_energy_total = 7.0078125
net_exp_energy_total = 0.19921875
```

### Encoder and datastore tests

- test `zero_low_word` with both supported word orders;
- assert the exact two Modbus words, not only the decoded float;
- verify that precise energy points retain their low words;
- verify all FC03 and FC04 addresses listed above;
- verify the constant-zero export-phase ranges;
- verify CT ratio `200` against the physical scan relationships;
- verify that FC04 `0x180A` quantity 22 and FC04 `0x1828` quantity 4 remain
  valid reads.

### Static-mode tests

- load the expanded `config/config.json`;
- build the static pipeline;
- assert negative active power and power factor at the Sigen output;
- assert non-zero active export and reactive energy;
- assert all derived aliases and constant-zero ranges;
- reject non-finite or unrepresentable configured values.

### Regression

Run the complete pytest suite and configuration load checks. Existing
measurement, identity, fail-safe, overflow, monitor, and transport behavior
must remain unchanged.

## Documentation

Update `docs/register-map.md` to:

- describe `0x180A` as total reactive energy;
- document the four-quadrant ND45 source calculation;
- describe `net_*` as physical directional aliases;
- record constant-zero phase-export registers;
- document coarse aliases and their zero low word;
- include the reverse-flow scan as validation evidence;
- list every independently configurable static-debug value.

