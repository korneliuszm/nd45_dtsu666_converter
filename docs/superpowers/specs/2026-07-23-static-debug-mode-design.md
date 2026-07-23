# Static Debug Mode Design

## Goal

Add an explicit application mode that serves stable, operator-configured
electrical values to Sigenergy without connecting to the ND45. This makes
register mapping, scaling, sign, and communication behavior observable without
rapidly changing source measurements.

## Command and safety boundary

The mode is started explicitly:

```bash
python -m nd45_dtsu666 static
```

`static` is separate from `run`, `monitor`, `rtudebug`, and `selftest`.
Production commands continue to poll the ND45 exactly as before. Merely adding
the static configuration does not enable or select test data.

The startup banner and dashboard clearly identify the source as
`STATIC DEBUG`, preventing test values from being mistaken for live ND45 data.

## Configuration

`config/config.json` gains:

```json
"static_debug": {
  "feed_interval_s": 0.5,
  "values": {
    "u_l1": 9000.0,
    "u_l2": 9000.0,
    "u_l3": 9000.0,
    "u_l12": 15588.0,
    "u_l23": 15588.0,
    "u_l31": 15588.0,
    "i_l1": 10.0,
    "i_l2": 20.0,
    "i_l3": 30.0,
    "p_l1": 10000.0,
    "p_l2": 20000.0,
    "p_l3": 30000.0,
    "p_total": 60000.0,
    "q_l1": 1000.0,
    "q_l2": 2000.0,
    "q_l3": 3000.0,
    "q_total": 6000.0,
    "pf_l1": 0.95,
    "pf_l2": 0.95,
    "pf_l3": 0.95,
    "pf_total": 0.95,
    "freq": 50.0
  }
}
```

`feed_interval_s` must be positive and shorter than
`safety.max_data_age_s`. Every configured value must be a finite number;
strings, booleans, NaN, and infinity are rejected during configuration load.

The accepted keys are canonical measurement names consumed by either output
map. Unknown names are rejected to catch spelling errors. Keys omitted from
`values` are supplied as `0.0`, including energy values.

## Architecture and data flow

The static mode uses the same output components as the live bridge:

1. Load and validate application and register configuration.
2. Expand configured static values to the full set of canonical target inputs,
   filling missing values with zero.
3. Build the standard FC03 and Sigen FC04 datastores, identity registers, and
   temporarily unidentified zero ranges.
4. Periodically write the unchanged canonical snapshot to the store and both
   measurement maps.
5. Run the existing freshness supervisor so transport startup, shutdown, and
   fail-safe behavior remain identical to production.

No Modbus TCP client is created and no connection to the ND45 is attempted.
The static mode never writes to the ND45.

## Dashboard and request activity

The terminal dashboard shows:

- `STATIC DEBUG` as the source and mode;
- configured phase values, totals, frequency, and direction;
- `SERVING` or fail-safe state;
- Sigenergy request count, rate, last-seen time, and commonly requested
  FC03/FC04 blocks.

It reuses `RtuActivity` and the existing monitor rendering behavior. A short
startup message identifies the loaded configuration file and reminds the
operator that all served measurements are synthetic.

## Error handling

Configuration errors stop startup before the Modbus transport is opened.
Transport failures and stale-data handling use the existing supervisor.
Ctrl-C and SIGTERM stop the feeder and server cleanly.

The mode does not silently derive totals or power factors from phase values.
Each output is exactly the configured value or zero when omitted, allowing
operators to test deliberately inconsistent patterns when diagnosing register
mapping.

## Testing

Automated tests verify:

- valid static configuration loads;
- unknown keys and non-finite or non-numeric values are rejected;
- missing canonical outputs are filled with zero;
- configured values are encoded into classic FC03 and Sigen FC04 with their
  respective scales;
- the static runner does not construct or connect an ND45 client;
- the feeder keeps the canonical store fresh;
- CLI dispatch selects `static`;
- the dashboard labels the source `STATIC DEBUG`;
- existing commands and tests retain their behavior.

## Non-goals

- Editing values interactively while the process runs.
- Automatically deriving totals, phase-to-phase voltages, or power factor.
- Persisting values changed at runtime.
- Replacing the live ND45 mode or the existing installation `selftest`.
