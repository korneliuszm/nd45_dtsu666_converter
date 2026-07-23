# ND45 data validity, finite float32, and forward-energy design

## Goal

Fix three safety and register-coverage defects in the ND45-to-DTSU666
converter:

1. an invalid critical ND45 value must not be replaced with zero and published
   as a fresh sample;
2. the converter must never encode IEEE-754 `NaN` or infinity in a DTSU666
   float32 register;
3. the actively polled Sigen register `0x180A` must expose the ND45 forward
   active-energy total, with its observed FC03 aliases.

The existing freshness fail-safe, output maps, CT-ratio model, and static debug
mode remain in place.

## Register interpretation

The live DTSU666 scans and the DTSU666 communication table identify:

- FC04 `0x180A` as current forward active total electric energy;
- FC03 `0x100A` and `0x1050` as secondary-side aliases of the same value;
- FC04 `0x180A` as the primary-side Sigen/OEM representation.

This corrects the earlier provisional description of `0x180A` as reactive
energy. The canonical ND45 source is therefore `imp_energy_total`, already
composed from ND45 registers `912` and `914` in kWh.

The target mappings are:

| Function | Address | Canonical source | CT handling |
|---|---:|---|---|
| FC03 | `0x100A` | `imp_energy_total` | divide by configured CT ratio |
| FC03 | `0x1050` | `imp_energy_total` | divide by configured CT ratio |
| FC04 | `0x180A` | `imp_energy_total` | no division; primary-side kWh |

No other formerly zero-filled register in `0x180A`–`0x181D` is inferred.

## ND45 sample validity

`poll_once()` continues to read all fixed register groups before decoding the
sample. Decoded values are classified as follows:

- `pf_l1`, `pf_l2`, `pf_l3`, and `pf_total` are optional/undefined-at-no-load
  points. A non-finite or ND45 over-range value for one of these is logged once
  per invalid episode and represented as `0.0`.
- Every other mapped source point is critical. If any critical value is
  non-finite or has absolute value greater than or equal to the ND45
  over-range sentinel threshold, the entire poll raises `PollError`.

All invalid critical point names are collected before raising, so one sample
reports the complete set of bad points. A sustained episode remains
rate-limited by the existing per-point warning state and `FaultReporter`.
Recovery of a point clears its warning state.

Because a failed poll never calls `on_update`, neither the canonical store nor
its timestamp is updated. The current maximum-data-age supervisor consequently
stops the DTSU server after the configured grace period instead of serving a
fabricated fresh zero.

## Finite float32 output contract

`float_to_registers()` accepts only values representable as finite IEEE-754
binary32:

- `NaN` and positive/negative infinity raise `ValueError`;
- finite values outside `[-FLOAT32_MAX, FLOAT32_MAX]` raise `ValueError`;
- finite in-range values retain the existing byte-order and word-order
  behavior.

Clamping is deliberately not used. A maximum finite float32 is still a
plausible Modbus value and would conceal a bad sample or configuration.

The guard is applied after target sign, scale, offset, and CT conversion,
because those transformations can make an otherwise valid canonical input
unrepresentable.

`update_datastore()` first stages every encoded target register payload in
memory and only starts writing after every value has passed validation. An
encoding failure therefore preserves the complete previous register image.
Live updates also leave the canonical store timestamp unchanged, so the
existing freshness fail-safe activates when that previous sample expires.

Static debug values are encoded once synchronously while
`build_static_pipeline()` is being built. Invalid configured values thus fail
startup immediately, before asynchronous tasks or the output server begin,
instead of failing later inside the feeder.

## Datastore consistency

The encode-first staging prevents deterministic validation errors from causing
a mixed old/new register image. This change does not make `setValues()` calls
or concurrent Modbus reads transactional; that broader synchronization change
is outside scope.

The static startup preflight may seed the context with the configured values.
The regular feeder will write the same deterministic register image and stamp
the canonical store once its coroutine starts.

## Tests

Tests will cover:

1. invalid PF remains zero without rejecting the sample;
2. invalid voltage/current/power/energy raises `PollError`;
3. a failed poll does not invoke the update callback or refresh its timestamp;
4. warning suppression and recovery continue to work;
5. `float_to_registers()` rejects `NaN`, infinities, and both signs of finite
   float32 overflow for every byte/word order;
6. the largest finite float32 remains encodable;
7. overflow caused by target scaling is rejected;
8. a failed staged encoding leaves all previously served registers unchanged;
9. static debug rejects an unencodable configured value during pipeline build;
10. FC04 `0x180A` serves unscaled `imp_energy_total`;
11. FC03 `0x100A` and `0x1050` serve `imp_energy_total / CT`;
12. surrounding unconfirmed gap registers remain zero and all active Sigen
    reads remain valid.

The full test suite and compile check must pass before completion.

## Documentation

The register map will be corrected to describe `0x180A` as forward active
energy, document its two FC03 aliases, and remove it from the list of
unimplemented reactive-energy registers.

## Out of scope

- inferring meanings for other zero-filled Sigen energy registers;
- remapping the existing `0x181E`–`0x1830` block;
- changing CT-ratio configuration or power-sign conventions;
- making datastore writes transactional;
- modifying the hardware polling interval or transport.
