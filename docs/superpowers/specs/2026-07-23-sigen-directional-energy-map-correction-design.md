# Sigen Directional Energy Map Correction Design

## Goal

Make the converter reproduce the physical Sigenergy DTSU666 energy image after
both active and reactive export counters have accumulated. Replace previously
zero-filled active-export and directional-reactive registers with canonical
values read from the ND45.

## Evidence

The physical-meter scan `scan_COM8_20260723_222502.txt` changes the conclusions
drawn from earlier scans:

- `0x1800` exposes combined active energy. Its coarse value is `10.0 kWh`,
  consistent with approximately `7.0 kWh` imported plus `3.0 kWh` exported.
- `0x1828` and `0x1830` expose the same total active-export counter.
- `0x182A`, `0x182C`, and `0x182E` become non-zero and track phase active
  export.
- The CT-side fields `0x1028` through `0x102E` expose the corresponding values
  divided by CT. With CT `200`, about `3.0 kWh` becomes about `0.015 kWh`.
- `0x180A` grew while instantaneous `Q` was negative and stopped growing after
  the sign changed. `0x1814` became non-zero while instantaneous `Q` was
  positive. `0x1850` aliases `0x180A`; `0x183C` aliases `0x1814`.
- The same directional-reactive pattern exists on the CT side at `0x100A`,
  `0x1014`, `0x103C`, and `0x1050`.

The DTSU666 manual confirms that `0x1028`, `0x102A`, `0x102C`, and `0x102E`
are total and per-phase reverse active energy. The ND45 manual identifies:

- `944/946` and `976/978` as the total imported (`Q+`) inductive and capacitive
  reactive counters;
- `960/962` and `992/994` as the total exported (`Q-`) inductive and capacitive
  reactive counters.

The scanner reads one register per request. Exact low words in dense float
blocks are therefore not used as mapping evidence; address presence, high
words, zero/non-zero transitions, CT relationships, aliases, and counter
direction are the binding evidence.

## Canonical Model

Keep the existing active-energy source points:

- `imp_energy_total`, `imp_energy_l1`, `imp_energy_l2`, `imp_energy_l3`;
- `exp_energy_total`, `exp_energy_l1`, `exp_energy_l2`, `exp_energy_l3`.

Replace the all-quadrant `reactive_energy_total` source with two independent
directional values:

```text
reactive_imp_energy_total =
    ND45[944] * 1000 + ND45[946]
  + ND45[976] * 1000 + ND45[978]

reactive_exp_energy_total =
    ND45[960] * 1000 + ND45[962]
  + ND45[992] * 1000 + ND45[994]
```

Every component remains critical. A non-finite or ND45 overrange component
rejects the complete poll before composition.

Continue deriving:

```text
active_energy_total = imp_energy_total + exp_energy_total
net_imp_energy_total = imp_energy_total
net_exp_energy_total = exp_energy_total
```

## Target Register Map

### FC03 CT-side energy

All active and reactive values in this section are divided by the configured
CT ratio.

| Address | Meaning | Canonical source | Encoding |
|---|---|---|---|
| `0x1000` | combined active energy | `active_energy_total` | coarse float |
| `0x100A` | exported reactive energy (`Q-`) | `reactive_exp_energy_total` | coarse float |
| `0x1014` | imported reactive energy (`Q+`) | `reactive_imp_energy_total` | coarse float |
| `0x101E` | total active import | `imp_energy_total` | float32 |
| `0x1020` | L1 active import | `imp_energy_l1` | float32 |
| `0x1022` | L2 active import | `imp_energy_l2` | float32 |
| `0x1024` | L3 active import | `imp_energy_l3` | float32 |
| `0x1026` | active-import alias | `net_imp_energy_total` | float32 |
| `0x1028` | total active export | `exp_energy_total` | float32 |
| `0x102A` | L1 active export | `exp_energy_l1` | float32 |
| `0x102C` | L2 active export | `exp_energy_l2` | float32 |
| `0x102E` | L3 active export | `exp_energy_l3` | float32 |
| `0x1030` | active-export alias | `net_exp_energy_total` | float32 |
| `0x103C` | imported-reactive alias (`Q+`) | `reactive_imp_energy_total` | coarse float |
| `0x1050` | exported-reactive alias (`Q-`) | `reactive_exp_energy_total` | coarse float |

### FC04 primary-side energy

The same meanings are exposed without CT division:

| Address | Meaning | Canonical source | Encoding |
|---|---|---|---|
| `0x1800` | combined active energy | `active_energy_total` | coarse float |
| `0x180A` | exported reactive energy (`Q-`) | `reactive_exp_energy_total` | coarse float |
| `0x1814` | imported reactive energy (`Q+`) | `reactive_imp_energy_total` | coarse float |
| `0x181E` | total active import | `imp_energy_total` | float32 |
| `0x1820` | L1 active import | `imp_energy_l1` | float32 |
| `0x1822` | L2 active import | `imp_energy_l2` | float32 |
| `0x1824` | L3 active import | `imp_energy_l3` | float32 |
| `0x1826` | active-import alias | `net_imp_energy_total` | float32 |
| `0x1828` | total active export | `exp_energy_total` | float32 |
| `0x182A` | L1 active export | `exp_energy_l1` | float32 |
| `0x182C` | L2 active export | `exp_energy_l2` | float32 |
| `0x182E` | L3 active export | `exp_energy_l3` | float32 |
| `0x1830` | active-export alias | `net_exp_energy_total` | float32 |
| `0x183C` | imported-reactive alias (`Q+`) | `reactive_imp_energy_total` | coarse float |
| `0x1850` | exported-reactive alias (`Q-`) | `reactive_exp_energy_total` | coarse float |

Every coarse float exposes the IEEE754 high word and forces its logical low
word to zero. This applies to ten addresses:

```text
0x1000, 0x100A, 0x1014, 0x103C, 0x1050,
0x1800, 0x180A, 0x1814, 0x183C, 0x1850
```

Only the remaining unmapped gaps stay zero.

## Static Debug Configuration

The static-debug allowlist must accept all independent ND45 source values,
including:

- `exp_energy_l1`, `exp_energy_l2`, `exp_energy_l3`;
- `reactive_imp_energy_total`;
- `reactive_exp_energy_total`.

It must reject the removed aggregate `reactive_energy_total` and continue
rejecting `active_energy_total`, `net_imp_energy_total`, and
`net_exp_energy_total`, because those values are derived.

The checked-in reverse-flow example will use non-zero per-phase export energy
whose sum explains the newly accumulated export, plus distinct non-zero `Q+`
and `Q-` energy values.

## Compatibility and Failure Behavior

- Keep the ND45 energy read as one block from `900` through `995`.
- Preserve component-level overrange validation and once-per-episode logging.
- Preserve encode-all-before-write datastore atomicity.
- Do not change transport, identity, current measurement, apparent-power,
  power-sign, fail-safe, or watchdog behavior.
- Existing Modbus requests remain address-valid; the change replaces incorrect
  zeros and sources with physical values.

## Testing

Tests will verify:

1. directional reactive composition and invalid-component rejection;
2. exact FC03 and FC04 target contracts;
3. all ten coarse encodings;
4. CT=200 relationships for total and phase active export;
5. physical reverse-flow values from the latest scan;
6. RTU-debug lookup names for the newly mapped fields;
7. static-debug acceptance, derivation, and end-to-end register output;
8. JSON validity, atomic-write regression, focused suites, and the complete
   portable test suite.

The six existing AF_UNIX-only tests remain excluded on this Windows runtime;
all other tests must pass without warnings.
