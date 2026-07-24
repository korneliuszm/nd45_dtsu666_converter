"""Query the bridge's own DTSU output server over TCP, reading the exact
blocks Sigenergy reads over FC04 (per docs/register-map.md), plus the FC03
identity block. Used to verify what the bridge actually serves without
touching the RS-485 hardware."""

from __future__ import annotations

import struct
import sys

from pymodbus.client import ModbusTcpClient

sys.path.insert(0, "src")
from nd45_dtsu666.config import load_registers  # noqa: E402


def f32(regs: list[int]) -> float:
    return struct.unpack(">f", struct.pack(">HH", regs[0], regs[1]))[0]


def main() -> int:
    registers = load_registers("config/registers.json")
    client = ModbusTcpClient("127.0.0.1", port=15020, timeout=2.0)
    if not client.connect():
        print("ERROR: could not connect to bridge TCP server")
        return 1

    try:
        print("FC04 dtsu_sigen_ext_target (every point, individually, per registers.json):")
        for key, pt in registers.dtsu_sigen_ext_target.points.items():
            rr = client.read_input_registers(pt.addr, 2, slave=10)
            if rr.isError():
                print(f"  {key:<12} addr={pt.addr:<6} ERROR {rr}")
            else:
                print(f"  {key:<12} addr={pt.addr:<6} = {f32(rr.registers):.4f}")

        print("FC04 dtsu_sigen_ext_energy (every point):")
        for key, pt in registers.dtsu_sigen_ext_energy.points.items():
            rr = client.read_input_registers(pt.addr, 2, slave=10)
            if rr.isError():
                print(f"  {key:<25} addr={pt.addr:<6} ERROR {rr}")
            else:
                print(f"  {key:<25} addr={pt.addr:<6} = {f32(rr.registers):.4f}")

        print("FC03 0x0003/qty5 (identity block Sigen reads):")
        rr = client.read_holding_registers(3, 5, slave=10)
        if rr.isError():
            print("  ERROR", rr)
        else:
            print(f"  {rr.registers}  (net, [unnamed], [unnamed], IrAt, UrAt)")

        print("FC03 0xF100/qty20 (model string):")
        rr = client.read_holding_registers(0xF100, 20, slave=10)
        if rr.isError():
            print("  ERROR", rr)
        else:
            raw = b"".join(struct.pack(">H", v) for v in rr.registers)
            print(f"  {raw.decode('ascii', errors='replace')!r}")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
