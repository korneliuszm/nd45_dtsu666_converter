"""Read each CHINT e2TANGO relay individually and compare them side by side.

The monitor and /metrics only ever show the etango bridge's *combined*
reading -- voltages/currents averaged, powers/energies summed. That hides the
single most likely field fault: a relay that answers Modbus normally but
reports zeros. Such a relay raises no error (etango_poller's strictness covers
read *failures*, not a valid response full of zeros), yet it silently drags the
summed power down and the averaged current with it.

This script talks to each relay separately so that case is visible, and prints
the per-device internal consistency checks the aggregate cannot show:

  * P vs 3 x U x I           -- is the total consistent with U and I?
  * S vs hypot(P, Q)         -- does the device's own S agree with its P/Q?

(No P vs P1+P2+P3 check: the e2TANGO-450 has no per-phase power register at
all -- see the DOC table below -- so registers.json's etango_source derives
p_l1..3/q_l1..3 as an equal split of the totals rather than reading them.)

Decoding goes through the project's own `codec.registers_to_float` with the
word/byte order taken from `config/registers.json`, so whatever this prints is
byte-for-byte what the bridge would decode -- a mismatch here is the device,
not the bridge.

Read-only, and safe to run against relays the service is already polling:
Modbus TCP allows several masters (unlike the RS-485 output side).

    python scripts/poll_etango.py
    python scripts/poll_etango.py --hosts 192.168.30.5,192.168.30.7 --dump
    python scripts/poll_etango.py --watch 2.0
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from pymodbus.client import ModbusTcpClient

sys.path.insert(0, "src")
from nd45_dtsu666.codec import registers_to_float  # noqa: E402
from nd45_dtsu666.config import load_config, load_registers  # noqa: E402

# addr -> (label, vendor unit). Mirrors ETANGO_DOC in tests/test_etango_poller.py;
# kept here as well so this script stands alone on a machine without the tests.
#
# e2TANGO-450 (firmware 9.1.1.x) specifically -- do not assume this matches
# the older e2TANGO (firmware 1.1.11.x): the map diverges after 0x001E. In
# particular 0x0036-0x0040 (54-64) holds per-phase ANGLES and symmetrical
# components on the -450, not per-phase P/Q as the older manual documented.
# There is no genuine per-phase active/reactive power register on this model
# at all -- registers.json's etango_source derives p_l1..3/q_l1..3 as an
# equal split of the totals instead of reading them.
DOC: list[tuple[int, str, str]] = [
    (0, "I1", "A"), (2, "I2", "A"), (4, "I3", "A"),
    (6, "U12", "V"), (8, "U23", "V"), (10, "U31", "V"),
    (12, "f", "Hz"),
    (14, "P", "W"), (16, "Q", "var"), (18, "S", "VA"), (20, "cos(phi)", "-"),
    (22, "tg(phi)", "-"),
    (24, "Ec+", "Wh"), (26, "Ec-", "Wh"), (28, "Eb+", "varh"), (30, "Eb-", "varh"),
    (48, "U1", "V"), (50, "U2", "V"), (52, "U3", "V"),
    (54, "phi1", "deg"), (56, "phi2", "deg"), (58, "phi3", "deg"),
    (60, "phiU1_2", "deg"), (62, "phiU1_3", "deg"), (64, "I_s1", "A"),
]

BLOCKS = ((0, 32), (48, 18))


def _default_devices() -> list[tuple[str, int, int]]:
    """The etango bridge's own device list from config.json, if present."""
    try:
        config = load_config("config/config.json")
    except Exception:  # noqa: BLE001 - the script must work without a config
        return []
    for bridge in config.bridges:
        if getattr(bridge.source, "type", None) == "etango":
            return [(d.host, d.port, d.unit_id) for d in bridge.source.devices]
    return []


def _orders() -> tuple[str, str]:
    """Word/byte order the bridge uses, so this decodes identically."""
    try:
        side = load_registers("config/registers.json").etango_source
        if side is not None:
            return side.word_order, side.byte_order
    except Exception:  # noqa: BLE001
        pass
    return "little", "big"  # etango_source's shipped CDAB order


def read_device(host: str, port: int, unit: int, timeout: float):
    """Return {addr: raw_register_pair} or an error string."""
    client = ModbusTcpClient(host, port=port, timeout=timeout)
    try:
        if not client.connect():
            return f"connect failed to {host}:{port}"
        flat: dict[int, int] = {}
        for base, count in BLOCKS:
            rr = client.read_input_registers(base, count, slave=unit)
            if rr.isError():
                return f"FC04 read error at {base} (unit {unit}): {rr}"
            if len(rr.registers) < count:
                return (
                    f"short read at {base}: got {len(rr.registers)} of {count}"
                )
            for offset, reg in enumerate(rr.registers):
                flat[base + offset] = reg
        return flat
    finally:
        client.close()


def decode(flat: dict[int, int], wo: str, bo: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for addr, _label, _unit in DOC:
        if addr in flat and addr + 1 in flat:
            out[addr] = registers_to_float([flat[addr], flat[addr + 1]], wo, bo)
    return out


def print_device(host: str, values: dict[int, float], flat, dump: bool) -> None:
    print(f"--- {host} " + "-" * (66 - len(host)))
    if dump:
        for base, count in BLOCKS:
            words = " ".join(f"{flat[base + i]:04X}" for i in range(count))
            print(f"  raw @{base:>3} x{count:<3}: {words}")
    line = []
    for addr, label, unit in DOC:
        if addr not in values:
            continue
        line.append(f"{label}={values[addr]:.4g}{unit if unit != '-' else ''}")
        if len(line) == 6:
            print("  " + "  ".join(line))
            line = []
    if line:
        print("  " + "  ".join(line))

    p, q, s = values.get(14), values.get(16), values.get(18)
    i1, i2, i3 = values.get(0), values.get(2), values.get(4)
    u1, u2, u3 = values.get(48), values.get(50), values.get(52)
    # No per-phase P1/P2/P3 check: this model has no such register (see the
    # DOC comment above) -- 54-64 are angles/symmetrical components, not
    # power, so there is nothing to cross-check them against here.

    print("  checks:")
    if None not in (p, u1, u2, u3, i1, i2, i3):
        calc = u1 * i1 + u2 * i2 + u3 * i3
        flag = "" if calc and abs(abs(p) / calc - 1) < 0.10 else "   <-- MISMATCH"
        print(f"    P={p:14.1f} W   sum(U*I)={calc:12.1f} VA  |P|/UI="
              f"{(abs(p) / calc if calc else float('nan')):.3f}{flag}")
    if None not in (p, q, s):
        h = math.hypot(p, q)
        flag = "" if h and abs(s / h - 1) < 0.05 else "   <-- MISMATCH"
        print(f"    S={s:14.1f} VA  hypot(P,Q)={h:10.1f} VA{flag}")
    if None not in (i1, i2, i3) and max(abs(i1), abs(i2), abs(i3)) < 1.0:
        print("    !! this relay reports ~ZERO CURRENT -- it contributes nothing")
        print("       to the summed power, and drags the averaged current down,")
        print("       without raising any error in the bridge.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hosts", help="comma-separated; default: config.json's etango devices")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=None, help="override every device's unit id")
    ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--dump", action="store_true", help="also print raw register words")
    ap.add_argument("--watch", type=float, default=0.0, help="repeat every N seconds")
    args = ap.parse_args()

    if args.hosts:
        devices = [(h.strip(), args.port, args.unit or 1) for h in args.hosts.split(",")]
    else:
        devices = _default_devices()
        if not devices:
            print("no etango devices in config/config.json; pass --hosts")
            return 1
        if args.unit is not None:
            devices = [(h, p, args.unit) for h, p, _u in devices]

    wo, bo = _orders()

    while True:
        print("=" * 72)
        print(f"e2TANGO per-device read   word_order={wo} byte_order={bo}   "
              f"{time.strftime('%H:%M:%S')}")
        print("=" * 72)
        decoded: list[tuple[str, dict[int, float]]] = []
        for host, port, unit in devices:
            result = read_device(host, port, unit, args.timeout)
            if isinstance(result, str):
                print(f"--- {host} " + "-" * (66 - len(host)))
                print(f"  ERROR: {result}")
                continue
            values = decode(result, wo, bo)
            print_device(host, values, result, args.dump)
            decoded.append((host, values))

        if decoded:
            print("--- combined (what the bridge serves) " + "-" * 34)
            live = [v for _h, v in decoded]
            p_sum = sum(v.get(14, 0.0) for v in live)
            q_sum = sum(v.get(16, 0.0) for v in live)
            i_avg = sum(v.get(0, 0.0) for v in live) / len(live)
            u_avg = sum(v.get(48, 0.0) for v in live) / len(live)
            print(f"    sum P = {p_sum:14.1f} W    sum Q = {q_sum:12.1f} var")
            print(f"    avg U1= {u_avg:14.2f} V    avg I1= {i_avg:12.2f} A")
            contributing = sum(1 for v in live if abs(v.get(0, 0.0)) >= 1.0)
            print(f"    relays reporting current: {contributing} of {len(live)}")
            if contributing and contributing < len(live):
                scale = len(live) / contributing
                print("    -> if the silent ones should be carrying comparable load,")
                print(f"       the true total is roughly {scale:.1f}x this, i.e. "
                      f"{p_sum * scale:.0f} W")

        if args.watch <= 0:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
