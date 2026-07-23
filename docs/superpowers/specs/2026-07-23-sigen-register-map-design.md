# Sigen Register Map Design

## Goal

Extend the ND45-to-DTSU666 bridge so Sigenergy can identify it as a
`Sigen Sensor TPX-CH` and poll the OEM Sigen measurement map, while preserving
the existing standard DTSU666 map for compatibility.

## Protocol behavior

The server will expose two independent Modbus data spaces:

- FC03 holding registers retain the existing DTSU666 measurement map and
  identity/configuration registers.
- FC04 input registers expose the Sigen OEM measurement map.

The configured slave ID and transport settings apply to both function codes.
The default deployment configuration remains RTU slave 10 at 9600 baud, 8N1.

## Classic DTSU666 map

The existing `dtsu_target` map remains available through FC03. Its addresses,
scales, byte order, word order, power-sign behavior, and energy registers do
not change.

This map remains the compatibility path for tools or masters that read the
standard Chint DTSU666 blocks.

## Sigen measurement map

`config/registers.json` gains a `dtsu_sigen_ext_target` section with
`function_code` set to 4. It mirrors the confirmed portion of the standard
measurement block at a register offset of `-2806` (`-0x0AF6`):

- voltages: `0x150A` through `0x1514`;
- currents: `0x1516` through `0x151A`;
- active power: `0x151C` through `0x1522`;
- reactive power: `0x1524` through `0x152A`;
- power factors: `0x1534` through `0x153A`;
- frequency: `0x154E`.

All entries are float32 values in big-endian word and byte order. Unlike the
classic DTSU666 map, their scale is `1`, so the values are encoded directly in
SI units. Existing power-direction signs are preserved.

Energy registers are not added to the Sigen map because their Sigen addresses
have not been observed. They remain available only through the confirmed
classic DTSU666 map.

## Sigen identity and handshake

FC03 holding registers will include the static OEM identity block:

- `0xF100` through `0xF113` form a fixed 40-byte ASCII field.
- Its content begins with `Sigen Sensor TPX-CH`, followed by a NUL byte.
- All remaining bytes in the field are zero.
- `0xF114` and `0xF115` contain `0x0000` and `0x1500`, respectively.

This produces the observed 32-bit big-endian handshake value `0x00001500`
when the master reads two registers starting at `0xF114`.

The static identity values are represented in register-map configuration
rather than embedded as opaque literals in request-handling code. The server
seeds them when it builds the datastore, alongside the existing low-address
DTSU666 identity registers.

## Configuration model

The register configuration model will support both target maps and their
function codes. The existing `dtsu_target` remains backward compatible when
no explicit function code is present by defaulting to FC03.

The Sigen map explicitly selects FC04. Static identity configuration contains
the ASCII field address and fixed register length plus the handshake address
and value. Configuration validation rejects unsupported function codes and
invalid static-field sizes.

## Data flow

The ND45 poller and canonical SI store do not change. On each successful
canonical update:

1. Values are encoded with the classic map's scales and written to FC03.
2. The same canonical values are encoded with scale `1` and written to FC04.
3. Static Sigen identity registers remain unchanged.

The freshness fail-safe continues to stop the entire Modbus server when ND45
data is stale, so neither map can expose stale measurements.

## Diagnostics

The existing request recorder already records the function code, address, and
count. Register-name lookup will include both target maps so FC04 Sigen reads
are labeled correctly without changing request timing or server behavior.

## Error handling

Configuration loading fails early for malformed target blocks, unsupported
function codes, overlapping point definitions within one function-code data
space, or a Sigen identity string that does not fit its configured field.

The existing server startup, retry, shutdown, and stale-data behavior remains
unchanged.

## Testing

Automated tests will verify:

- the seed configuration loads both target maps with FC03 and FC04;
- the exact 20 holding-register words at `0xF100`;
- the exact handshake words at `0xF114`;
- FC03 and FC04 use independent datastore blocks;
- one canonical value is encoded with classic scaling in FC03 and direct
  scaling in FC04;
- all configured Sigen measurement addresses match the specified offset;
- request diagnostics distinguish FC03 from FC04;
- existing classic-map, identity, transport, supervisor, and fail-safe tests
  continue to pass.

Hardware commissioning must still confirm live Sigenergy acceptance, phase
order, power direction, and the inferred current/per-phase power mappings under
load. Those observations cannot be proven by datastore-level tests.

## Non-goals

- Inferring unobserved Sigen energy-register addresses.
- Removing or replacing the classic DTSU666 map.
- Changing ND45 polling or the canonical measurement model.
- Claiming hardware validation of inferred registers without a loaded-meter
  capture.
