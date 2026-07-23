# DTSU666 Sigen CT-ratio and extended-energy design

## Source

A field reverse-engineering report (passive RS-485 sniff of a live Sigenergy
install talking to a real Chint DTSU666, plus active full-range register
scans under no-load and 3-phase load, cross-checked against energy
accumulation over a 68-minute window) corrected two assumptions the previous
Sigen register-map design (`2026-07-23-sigen-register-map-design.md`) made
without hardware access:

1. The classic DTSU666 map (FC03, `0x2000`/`0x101E`) is **secondary-side**
   (post-CT) — its current, power, and energy registers must be divided by
   the CT ratio. Voltage, power factor, and frequency are unaffected (no CT
   in their signal path).
2. The Sigen OEM map (FC04, `0x150A`) is **primary-side** and reports active/
   reactive/apparent power in **kW/kvar/kVA**, not W/var/VA. Voltage,
   current, power factor, and frequency there are already SI values (x1).

`IrAt` (holding register `0x0006`) was previously assumed x0.1-scaled (raw
10 -> ratio 1.0), matching `UrAt`. The report's independent verifications
(current, power, and — most convincingly — energy-accumulation balance
across two readings 68 minutes apart) all landed on exactly the same factor
whether or not treated as x0.1, but only when using the RAW register value
directly as the ratio (this install's meter has `IrAt = 200`, ratio 200, not
20). `IrAt` is therefore used directly, unlike `UrAt`.

`config/config.json`'s `dtsu.identity.ir_at` already held `200` for this
meter — the value was correct, but nothing in the translator actually used
it to scale measurements before this change; it was cosmetic (written to the
identity register, never applied to CT-ratio scaling).

## ND45 side: primary-side, confirmed

The ND45 already reports converted (primary-side) current/power/energy — it
applies its own configured CT ratio internally before exposing values over
Modbus, so `nd45_source` values need no further primary/secondary
adjustment. This resolves the open question the source report flagged in its
section 4.6: `divide_by_ct` on the classic map and the direct pass-through on
the Sigen OEM map are both correct as implemented, not merely a working
assumption. What remains unconfirmed on-site is unrelated to this: sign
convention, phase order, and `exp_ep` behavior under real export — see the
README checklist.

## Changes

- `TargetPoint` (config.py) gains `divide_by_ct: bool = False`. Classic
  DTSU666 points for `i_*`, `p_*`, `q_*`, and all energy accumulators set it;
  voltage/PF/freq and every Sigen OEM point (already primary) do not.
- `update_datastore` (dtsu_server.py) takes a `ct_ratio` parameter (default
  `1.0`, a no-op) and divides `divide_by_ct` points by it before scaling.
  Callers pass `config.dtsu.identity.ir_at` — the existing CT-ratio config
  field becomes load-bearing instead of cosmetic.
- `dtsu_sigen_ext_target`'s power/reactive-power points (`p_*`, `q_*`) change
  scale from `1` to `0.001` (W/var -> kW/kvar).
- The temporary `dtsu_sigen_zero_ranges` static-zero mechanism is replaced by
  a real `dtsu_sigen_ext_energy` target (FC04, offset `+0x800`/`+2048` from
  the classic energy block): `imp_ep`, per-phase import, `net_imp_ep`,
  `exp_ep`, per-phase export, `net_exp_ep`, all primary-side kWh (`scale: 1`,
  no CT division). The unconfirmed reactive-energy accumulator at `0x180A`
  (no ND45 source) and other unnamed addresses in the polled ranges stay
  zero-filled the same way any unmapped `TargetPoint` gap already does —
  no special-cased "zero range" concept is needed anymore.
- `write_static_registers` adds holding register `0x0046 = 0`, observed
  polled by Sigenergy every ~5.4s alongside the config block.
- `DtsuIdentityConf.ir_at` gets a `> 0` validator since it is now a divisor.

## Apparent power and config registers (from live-meter scan)

A full holding-register scan of the real TPX-CH meter (slave 10) confirmed the
maps above bit-for-bit and surfaced additions:

- **Apparent power S** (St/Sa/Sb/Sc) at classic `0x2022`–`0x2028` and Sigen
  FC04 `0x152C`–`0x1532`. Sigenergy reads these in its `0x1528`/qty14 block, so
  the converter must serve them instead of zeros. `S = |U·I|` per phase,
  `s_total = Σ phases` (arithmetic apparent power, matching the meter), computed
  in `nd45_poller.compute_derived()` alongside net energy and served on both
  maps (classic ×10 /CT, Sigen ×0.001 kVA).
- **Config registers** `0x0004 = 1` and `0x0008 = 4` observed on the real
  meter (0x0004 is inside Sigenergy's `0x0003`/qty5 config read) are seeded as
  fixed constants in `write_static_registers`; `disp`/`b_lcd`/`endian`
  (`0x000A`–`0x000C` = 10/1/4) are set via `dtsu.identity` in config.

The full verified layout with addresses, descriptions and multipliers lives in
`docs/register-map.md`.

## Non-goals

- Changing the ND45 source register map or canonical model.
- Inferring the unconfirmed reactive-energy accumulator's ND45 source.
- Confirming `exp_ep` behavior under real export — remains an on-site
  verification item (primary-vs-secondary ND45 reporting is now confirmed,
  see above).
