# nd45_dtsu666_converter

Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map, served as
either Modbus RTU or Modbus TCP (config-selectable) so a Sigenergy storage system can
read it as a "Power Sensor".

The output exposes both the standard DTSU666 holding-register map over FC03 (secondary/
CT side, `0x2000`/`0x101E`) and the Sigenergy OEM map over FC04 (primary side, `0x150A`
measurements in SI units except power/reactive power in kW/kvar). Both sides derive from
the same ND45 reading; the classic map divides current/power/energy by the configured CT
ratio (`dtsu.identity.ir_at`) while the Sigen map does not (already primary). It also
serves the FC03 identity string `Sigen Sensor TPX-CH` at `0xF100` and the observed
`0x00001500` handshake at `0xF114`. Apparent power is read directly from ND45
registers `60`/`84`/`108` (L1/L2/L3) and `132` (L123 sum), then served on both maps
(classic `0x2022`, Sigen FC04 `0x152C`).

The Sigen FC04 energy map reproduces the physical TPX-CH behavior rather than
assuming a uniform `+0x800` copy of the generic DTSU666 map. `0x180A` is the
coarse total reactive-energy accumulator built from all four ND45 reactive
quadrants; `0x181E` is precise active import; `0x1828` is precise active
export. The classic FC03 energy aliases are CT-side values. Confirmed
phase-export fields remain zero, while the directional `net_*` fields repeat
their corresponding import/export totals as on the physical meter.

The full verified register layout — every address, description and multiplier, checked
against a live-meter scan — is in [`docs/register-map.md`](docs/register-map.md).

## Install (reComputer R1000, Ubuntu)
```bash
sudo mkdir -p /opt/nd45_dtsu666 && sudo chown $USER /opt/nd45_dtsu666
git clone https://github.com/korneliuszm/nd45_dtsu666_converter.git /opt/nd45_dtsu666
cd /opt/nd45_dtsu666
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Edit `config/config.json`: set ND45 `host`, and under `dtsu` pick the output
`transport`:
- `"rtu"`: fill in `dtsu.rtu` — the RS-485 device `port` (`/dev/ttyAMA2` etc.),
  `baudrate`, `parity`, `stopbits`.
- `"tcp"`: fill in `dtsu.tcp` — `host` to bind (`0.0.0.0` for all interfaces) and `port`
  (502 is the Modbus TCP default).

`dtsu.slave_id` applies to both transports (RS-485 slave address for RTU, unit id in
the MBAP header for TCP) and must match what Sigenergy is configured to poll.

## Bench test before connecting Sigenergy
Run the configured transport with synthetic data and read it with mbpoll.

RTU (`dtsu.transport: "rtu"`):
```bash
python -m nd45_dtsu666 selftest
# in another shell (RTU master), read holding registers as float from address 8192:
mbpoll -m rtu -a 1 -b 9600 -P none -t 4:float -r 8193 -c 4 /dev/ttyUSB0
```

TCP (`dtsu.transport: "tcp"`):
```bash
python -m nd45_dtsu666 selftest
# in another shell (TCP master), read holding registers as float from address 8192:
mbpoll -m tcp -a 1 -t 4:float -r 8193 -c 4 127.0.0.1 -p 502
```
(Note mbpoll `-r` is 1-based; register 8192 → `-r 8193`. Confirm word order matches.)

## Diagnostics
```bash
python -m nd45_dtsu666 diag   # live table: canonical SI, DTSU addr/raw, age, status
```

## Interactive monitor (commissioning)
Runs the live bridge (poller + DTSU output server + fail-safe) **and** shows a live
dashboard: the ND45 values per phase (with IMPORT/EXPORT power direction — the key
sign-convention check) plus the Modbus read requests coming from Sigenergy (count, rate,
which register blocks it reads), over whichever transport is configured. Use this
instead of `run` while bringing the system up.
```bash
python -m nd45_dtsu666 monitor   # Ctrl-C to quit
```
Requires the configured output transport to be reachable (real RS-485 port for `"rtu"`,
a free TCP port for `"tcp"`) — same requirement as `run`; for a bench test without
Sigenergy use `selftest`.
During fail-safe (stale ND45) the panel shows `FAIL-SAFE SILENT` — that is expected.

## Debug: which registers does Sigenergy read?
For raw protocol debugging (no dashboard), `rtudebug` runs the same live bridge but
logs one line per read request from Sigenergy — function code, address (decimal + hex),
word count, and the DTSU register name(s) that block touches — after printing the full
`addr → name` map once at startup. It reuses the same non-invasive read hook as
`monitor`, so it does not affect the standard `run` service. Redirect to a file to keep a
capture:
```bash
python -m nd45_dtsu666 rtudebug > rtu_debug.log 2>&1   # Ctrl-C to quit
```

## Static debug values for Sigenergy

Use `static` when rapidly changing ND45 measurements make commissioning hard.
This mode does not connect to the ND45. It continuously serves the fixed values
from `static_debug.values` in `config/config.json` through the same classic
FC03 and Sigen FC04 maps as the live bridge:

```bash
python -m nd45_dtsu666 static
```

The dashboard is labeled `STATIC DEBUG` and still shows the blocks requested by
Sigenergy. Edit the JSON values before startup; omitted measurements are served
as zero, except omitted phase apparent power (`s_l1/s_l2/s_l3`) is calculated
from the configured U and I, and omitted `s_total` is their sum. Explicit `s_*`
values are served unchanged. Values must be finite JSON numbers, and unknown
names stop startup so spelling mistakes cannot silently produce zeros.

Independent static inputs are voltages, currents, active/reactive/apparent power,
PF, frequency, active-import total and phase counters, active-export total, and
total reactive energy. All other energy outputs are fixed zero fields or derived
from those independent values.

The checked-in example represents export: active power and PF are negative,
`exp_energy_total` is non-zero, and `reactive_energy_total` drives the coarse
reactive aliases. `active_energy_total`, both `net_*` values, CT-side values,
and coarse aliases are derived automatically. Confirmed zero-only phase
export registers cannot be configured.

Only one process can own an RTU serial port. Stop the normal service or monitor
before starting static mode:

```bash
sudo systemctl stop nd45-dtsu666
python -m nd45_dtsu666 static
# Ctrl-C when finished
sudo systemctl start nd45-dtsu666
```

Static mode is strictly opt-in: the `run`, `monitor`, `rtudebug`, and `selftest`
commands continue to use their existing data sources.

## Run as a service
```bash
sudo cp systemd/nd45-dtsu666.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nd45-dtsu666
journalctl -u nd45-dtsu666 -f
```

The unit uses systemd's watchdog (`WatchdogSec=90`): the app pings systemd every
~45s as long as the ND45 poller is making progress (connecting, polling
successfully, or handling a poll failure — a real ND45 outage is a normal,
expected state, not a hang). If the poller genuinely freezes for 90s, systemd
stops seeing pings and restarts the service automatically (`Restart=always`
already covers this the same as a crash). No config changes needed to enable
or disable this — it's entirely controlled by `WatchdogSec=` in the unit file.

## On-site verification checklist (before leaving unattended)
1. **Sign convention** — with known import/export, confirm Sigenergy sees correct grid
   direction. If reversed, set `sign: -1` on the `p_*` target points in `registers.json`.
2. **Phase order** — confirm L1/L2/L3 == A/B/C. Swap `from` keys if needed.
3. **Scaling** — voltages/currents/power read plausibly on Sigenergy.
4. **Word/byte order** — if values look garbled, flip `word_order`/`byte_order` in the map.
5. **Slave ID / transport params** — match what Sigenergy polls: slave/unit id always;
   baud/parity/stopbits for RTU, or host/port for TCP.
6. **Fail-safe** — pull the ND45 network cable; confirm the output server goes silent
   (`journalctl` shows "fail-safe") and Sigenergy enters its safe mode.
7. **RS-485 direction** (RTU transport only) — verify the reComputer transceiver
   auto-toggles direction, or configure pyserial RS-485 mode if the master sees
   no/garbled replies. Not applicable when `dtsu.transport` is `"tcp"`.
8. **Identity/config registers (0x0000-0x002E)** — values are set from `dtsu.identity` in
   `config/config.json` (defaults: direct-connect 3P4W meter, CT ratio 1:1 — `net=0`,
   `ir_at=ur_at=10`). `ir_at` is **not** cosmetic: it is the actual CT ratio the classic
   DTSU666 map (FC03) divides current/power/energy by, since that map is secondary-side
   while the ND45 source (already converted internally by the ND45) and the Sigen OEM map
   (FC04) are primary-side — get it wrong and every classic-map value is off by that
   factor. To match a specific real meter, edit `dtsu.identity` by hand, e.g.:
   ```json
   "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10}
   ```
   Fields omitted keep their default. See `DtsuIdentityConf` in `config.py` for the full
   field list and `_IDENTITY_REGISTER_ADDRS` in `dtsu_server.py` for the register mapping
   (background: `docs/superpowers/specs/2026-07-23-dtsu-sigen-ct-ratio-design.md`).
9. **Sigen OEM registers** — confirm the storage reads FC04 `0x151C` for total active
   power (in kW, not W) and periodically reads FC03 `0xF114` for the identity handshake.
   The configured FC04 current and per-phase power positions follow the confirmed
   `-0x0AF6` block offset, and the energy block follows the confirmed `+0x800` offset from
   the classic energy registers, but both still require an on-site capture under load —
   in particular whether `exp_ep` (export energy) behaves correctly, since it was never
   observed non-zero in the source capture (no generation source on the test bench).
