"""Full raw register scan of the physical DTSU666 (Sigen Sensor) meter over
Modbus RTU, in the same per-address format as the DTSU_scans/*.txt dumps
from the previous session (ADDR/HEX/RAW/UINT16/paired-FLOAT32 columns).

Reads in bulk blocks (<=100 registers/request) for speed, then renders one
line per address exactly like the earlier manual scans. Uses FC03 (holding)
since the real meter answers both the classic 0x0000-0x2100 range and the
FC04-documented Sigen OEM addresses (0x150A.., 0x1800..) under FC03 too.

    .venv\\Scripts\\python.exe scripts\\full_scan_dtsu666.py --port COM8
"""

from __future__ import annotations

import argparse
import struct
import time
from pathlib import Path

from pymodbus.client import ModbusSerialClient

BLOCK = 20  # registers per request when the range is fully defined


def f32(hi: int, lo: int) -> float:
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def _read_block(client: ModbusSerialClient, slave: int, addr: int, count: int) -> list[int] | None:
    rr = client.read_holding_registers(addr, count, slave=slave)
    if rr.isError():
        return None
    return rr.registers


def scan_span(
    client: ModbusSerialClient, slave: int, addr: int, count: int, values: dict[int, int]
) -> None:
    """Read [addr, addr+count) into `values`, bisecting on IllegalAddress so a
    single undefined register inside a block doesn't drop the whole block --
    the real meter rejects any block touching an unimplemented address."""
    regs = _read_block(client, slave, addr, count)
    if regs is not None:
        for i, v in enumerate(regs):
            values[addr + i] = v
        return
    if count == 1:
        return  # genuinely undefined register; silently skip
    half = count // 2
    scan_span(client, slave, addr, half, values)
    scan_span(client, slave, addr + half, count - half, values)


def scan_range(client: ModbusSerialClient, slave: int, start: int, end: int) -> dict[int, int]:
    values: dict[int, int] = {}
    addr = start
    while addr < end:
        count = min(BLOCK, end - addr)
        scan_span(client, slave, addr, count, values)
        addr += count
    return values


def render(values: dict[int, int], start: int, end: int) -> list[str]:
    lines = [
        "  ADDR(dec)   ADDR(hex)   RAW    UINT16   FLOAT32(this,next)",
    ]
    for addr in range(start, end):
        if addr not in values:
            continue
        raw = values[addr]
        if addr + 1 in values:
            fl = f32(raw, values[addr + 1])
            fl_txt = repr(fl)
        else:
            fl_txt = "(no pair)"
        lines.append(
            f"  {addr:<11} {hex(addr):<11} {raw:04X}   {raw:<8} {fl_txt}"
        )
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM8")
    ap.add_argument("--slave", type=int, default=10)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--start", type=lambda s: int(s, 0), default=0x0000)
    ap.add_argument("--end", type=lambda s: int(s, 0), default=0x2100)
    ap.add_argument("--out-dir", default="DTSU_scans")
    ap.add_argument(
        "--exhaustive", action="store_true",
        help="bisect the whole --start..--end span instead of just the known register blocks "
             "(slow: the meter rejects any block touching an unimplemented address)",
    )
    args = ap.parse_args()

    # Known-populated blocks per docs/register-map.md, confirmed by the prior
    # session's exhaustive scan (DTSU_scans/scan_COM8_20260723_*.txt covered the
    # full 0x0000-0x2100 span and found exactly these five blocks + identity).
    KNOWN_BLOCKS = [
        (0x0000, 0x0047),   # identity/config
        (0x1000, 0x1032),   # classic FC03 energy + aliases
        (0x150A, 0x1550),   # Sigen OEM measurements
        (0x1800, 0x1852),   # Sigen OEM energy
        (0x2000, 0x2046),   # classic FC03 measurements
    ]

    client = ModbusSerialClient(
        port=args.port, baudrate=args.baud, parity="N", stopbits=1,
        bytesize=8, timeout=1.0,
    )
    if not client.connect():
        print(f"ERROR: could not open {args.port}")
        return 1

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"Scanning {args.port} slave={args.slave} range={hex(args.start)}-{hex(args.end)} ...")
    try:
        values: dict[int, int] = {}
        if args.exhaustive:
            values = scan_range(client, args.slave, args.start, args.end)
            print(f"  main range: {len(values)}/{args.end - args.start} registers read")
        else:
            for b_start, b_end in KNOWN_BLOCKS:
                block_values = scan_range(client, args.slave, b_start, b_end)
                values.update(block_values)
                print(
                    f"  block {hex(b_start)}-{hex(b_end)}: "
                    f"{len(block_values)}/{b_end - b_start} registers read"
                )

        # Sigen OEM identity block lives far above the main range (0xF100/0xF114).
        identity = scan_range(client, args.slave, 0xF100, 0xF116)
        print(f"  identity block: {len(identity)}/22 registers read")
    finally:
        client.close()

    out_lines = [
        "Modbus holding-register scan",
        f"port={args.port}  slave={args.slave}  baud={args.baud} 8N1  "
        f"range={hex(args.start)}-{hex(args.end)}",
        f"started={started}",
        "",
    ]
    out_lines += render(values, args.start, args.end)
    out_lines.append("")
    out_lines.append("Identity block (0xF100-0xF115):")
    out_lines += render(identity, 0xF100, 0xF116)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"scan_{args.port}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(out_lines)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
