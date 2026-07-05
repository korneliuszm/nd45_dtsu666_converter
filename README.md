# nd45_dtsu666_converter

Temporary Modbus bridge: Lumel ND45 (Modbus TCP) → DTSU666 register map (Modbus RTU)
so a Sigenergy storage system can read it as a "Power Sensor".

## Install (reComputer R1000, Ubuntu)
```bash
sudo mkdir -p /opt/nd45_dtsu666 && sudo chown $USER /opt/nd45_dtsu666
git clone https://github.com/korneliuszm/nd45_dtsu666_converter.git /opt/nd45_dtsu666
cd /opt/nd45_dtsu666
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Edit `config/config.json`: set ND45 `host`, the RS-485 device `port` (`/dev/ttyAMA2` etc.),
`slave_id`, and `baudrate`.

## Bench test before connecting Sigenergy
Run the RTU side with synthetic data and read it with mbpoll:
```bash
python -m nd45_dtsu666 selftest
# in another shell (RTU master), read holding registers as float from address 8192:
mbpoll -m rtu -a 1 -b 9600 -P none -t 4:float -r 8193 -c 4 /dev/ttyUSB0
```
(Note mbpoll `-r` is 1-based; register 8192 → `-r 8193`. Confirm word order matches.)

## Diagnostics
```bash
python -m nd45_dtsu666 diag   # live table: canonical SI, DTSU addr/raw, age, status
```

## Interactive monitor (commissioning)
Runs the live bridge (poller + RTU server + fail-safe) **and** shows a live dashboard:
the ND45 values per phase (with IMPORT/EXPORT power direction — the key sign-convention
check) plus the Modbus RTU read requests coming from Sigenergy (count, rate, which register
blocks it reads). Use this instead of `run` while bringing the system up.
```bash
python -m nd45_dtsu666 monitor   # Ctrl-C to quit
```
Requires the real RS-485 port (like `run`); for a bench test without Sigenergy use `selftest`.
During fail-safe (stale ND45) the RTU panel shows `FAIL-SAFE SILENT` — that is expected.

## Run as a service
```bash
sudo cp systemd/nd45-dtsu666.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nd45-dtsu666
journalctl -u nd45-dtsu666 -f
```

## On-site verification checklist (before leaving unattended)
1. **Sign convention** — with known import/export, confirm Sigenergy sees correct grid
   direction. If reversed, set `sign: -1` on the `p_*` target points in `registers.json`.
2. **Phase order** — confirm L1/L2/L3 == A/B/C. Swap `from` keys if needed.
3. **Scaling** — voltages/currents/power read plausibly on Sigenergy.
4. **Word/byte order** — if values look garbled, flip `word_order`/`byte_order` in the map.
5. **Slave ID / baud** — match what Sigenergy polls.
6. **Fail-safe** — pull the ND45 network cable; confirm the RTU side goes silent
   (`journalctl` shows "fail-safe") and Sigenergy enters its safe mode.
7. **RS-485 direction** — verify the reComputer transceiver auto-toggles direction, or
   configure pyserial RS-485 mode if the master sees no/garbled replies.
8. **Identity/config registers (0x0000-0x002E)** — served as a direct-connect 3P4W meter
   with CT/PT ratio 1:1 (`net=0`, `IrAt=UrAt=10`); see `_STATIC_INT16_REGISTERS` in
   `dtsu_server.py`. If Sigenergy rejects the meter or misapplies scaling, check these
   against the DTSU666 manual — this hasn't been confirmed against real Sigenergy behavior.
