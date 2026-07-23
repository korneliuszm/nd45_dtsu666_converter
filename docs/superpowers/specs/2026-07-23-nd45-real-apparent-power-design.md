# ND45 Real Apparent Power Design

## Goal

Use the apparent-power measurements reported directly by the ND45 instead of
reconstructing them from phase voltage and current.

## Source registers

The ND45 user manual, section 12.8.4 (200 ms aggregation), defines these
holding-register values as IEEE-754 float32 in VA:

- `0060`: apparent power L1
- `0084`: apparent power L2
- `0108`: apparent power L3
- `0132`: sum of apparent power L123

All four addresses already fall within the existing `50..145` read block, so
the change must not add another Modbus request.

## Data flow

Add `s_l1`, `s_l2`, `s_l3`, and `s_total` to `nd45_source` in
`config/registers.json`. `poll_once()` will decode them like the other direct
ND45 float32 measurements. `compute_derived()` will continue to derive net
energy only and must not overwrite apparent power.

The existing DTSU FC03 and Sigen FC04 targets already consume the four
canonical `s_*` values. Their target addresses and scaling remain unchanged.
The total exposed to Sigenergy must be the ND45 value from register `0132`,
not the arithmetic sum of phase values.

## Static debug mode

Static debug values are not ND45 readings. Preserve its convenient fallback:
when an `s_*` value is omitted, derive that one value from configured phase
voltage/current, and derive an omitted `s_total` as the sum of the three
effective phase apparent powers. Explicitly configured `s_*` values must win
and must never be overwritten.

## Verification

Add a poller regression test with ND45 apparent-power values deliberately
different from `U × I`. It must prove all four source values, including the
independent total, survive polling unchanged. Update static-debug tests to
prove both fallback derivation and explicit-value preservation. Run the
complete test suite after updating documentation.
