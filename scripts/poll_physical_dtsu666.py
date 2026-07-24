"""Standalone diagnostic: poll the physical DTSU666 (Sigen Sensor) meter on
COM8 over Modbus RTU and decode its FC04 (Sigen OEM, primary-side) and FC03
(classic, CT-side) measurement blocks into engineering units.

Not part of the package; run directly with the project venv:

    .venv\\Scripts\\python.exe scripts\\poll_physical_dtsu666.py [--port COM8] [--slave 10]

This talks to a REAL physical meter, not the bridge -- used to verify sign
convention, scaling and live values independently of nd45_dtsu666's own code.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time

from pymodbus.client import ModbusSerialClient


def f32(regs: list[int]) -> float:
    raw = struct.pack(">HH", regs[0], regs[1])
    return struct.unpack(">f", raw)[0]


# (label, addr, register_count, decode) -- addr/count per docs/register-map.md
FC04_MEASUREMENTS = [
    ("Uab", 5386, "V"), ("Ubc", 5388, "V"), ("Uca", 5390, "V"),
    ("Ua", 5392, "V"), ("Ub", 5394, "V"), ("Uc", 5396, "V"),
    ("Ia", 5398, "A"), ("Ib", 5400, "A"), ("Ic", 5402, "A"),
    ("Pt", 5404, "kW"), ("Pa", 5406, "kW"), ("Pb", 5408, "kW"), ("Pc", 5410, "kW"),
    ("Qt", 5412, "kvar"), ("Qa", 5414, "kvar"), ("Qb", 5416, "kvar"), ("Qc", 5418, "kvar"),
    ("St", 5420, "kVA"), ("Sa", 5422, "kVA"), ("Sb", 5424, "kVA"), ("Sc", 5426, "kVA"),
    ("PFt", 5428, ""), ("PFa", 5430, ""), ("PFb", 5432, ""), ("PFc", 5434, ""),
    ("Freq", 5454, "Hz"),
]
FC04_ENERGY = [
    ("ImpEp total", 6174, "kWh"), ("ExpEp total", 6184, "kWh"),
]
FC03_IDENTITY = [
    ("REV", 0x0000), ("UCode", 0x0001), ("net (3P4W=0)", 0x0003),
    ("IrAt (CT ratio)", 0x0006), ("UrAt (x0.1->1.0)", 0x0007),
]


def read_f32(client: ModbusSerialClient, slave: int, addr: int) -> float | None:
    rr = client.read_input_registers(addr, 2, slave=slave)
    if rr.isError():
        return None
    return f32(rr.registers)


def read_u16(client: ModbusSerialClient, slave: int, addr: int) -> int | None:
    rr = client.read_holding_registers(addr, 1, slave=slave)
    if rr.isError():
        return None
    return rr.registers[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--slave", type=int, default=10)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args()

    client = ModbusSerialClient(
        port=args.port, baudrate=args.baud, parity="N", stopbits=1,
        bytesize=8, timeout=1.0,
    )
    if not client.connect():
        print(f"ERROR: could not open {args.port}", file=sys.stderr)
        return 1

    try:
        print(f"Identity (FC03 holding, slave {args.slave}):")
        for label, addr in FC03_IDENTITY:
            val = read_u16(client, args.slave, addr)
            print(f"  {label:<20} = {val}")
        print()

        while True:
            print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} FC04 measurements (primary side) ---")
            for label, addr, unit in FC04_MEASUREMENTS:
                val = read_f32(client, args.slave, addr)
                val_txt = "ERR" if val is None else f"{val:.4f}"
                print(f"  {label:<5} = {val_txt:>12} {unit}")
            print("--- FC04 energy ---")
            for label, addr, unit in FC04_ENERGY:
                val = read_f32(client, args.slave, addr)
                val_txt = "ERR" if val is None else f"{val:.4f}"
                print(f"  {label:<15} = {val_txt:>12} {unit}")
            print()
            if args.once:
                break
            time.sleep(2.0)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
