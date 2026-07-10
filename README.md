# nd45_dtsu666_converter

Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map, served as
either Modbus RTU or Modbus TCP (config-selectable) so a Sigenergy storage system can
read it as a "Power Sensor".

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
   `config/config.json` (defaults: direct-connect 3P4W meter, CT/PT ratio 1:1 — `net=0`,
   `ir_at=ur_at=10`). To match a specific real meter, edit `dtsu.identity` by hand, e.g.:
   ```json
   "identity": {"rev": 103, "ucode": 701, "ir_at": 200, "ur_at": 10}
   ```
   Fields omitted keep their default. See `DtsuIdentityConf` in `config.py` for the full
   field list and `_IDENTITY_REGISTER_ADDRS` in `dtsu_server.py` for the register mapping.
   If Sigenergy rejects the meter or misapplies scaling, check these against the DTSU666
   manual — this hasn't been confirmed against real Sigenergy behavior.
