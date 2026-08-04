"""Diagnostic table renderer + diag/selftest command runners."""

from __future__ import annotations

import asyncio
import math
import time

from .canonical import CanonicalStore, HealthGate, compute_derived
from .codec import registers_to_float
from .config import AppConfig, BridgeConf, RegisterMap, load_config, load_registers
from .dtsu_server import encode_target_point


def render_table(
    source,
    target,
    canonical: dict[str, float],
    age: float,
    healthy: bool,
    ct_ratio: float = 1.0,
) -> str:
    status = "OK" if healthy else "STALE/FAILSAFE"
    lines = [
        f"status: {status}   data age: {age:.2f}s",
        f"{'canonical':<18}{'SI value':>14}   {'DTSU addr':>9}{'reg raw':>14}",
        "-" * 60,
    ]
    for key, pt in target.points.items():
        si = canonical.get(pt.from_)
        if si is None:
            si_txt, raw_txt = "-", "-"
        else:
            regs = encode_target_point(si, pt, target, ct_ratio=ct_ratio)
            si_txt = f"{si:.3f}"
            raw_txt = f"{registers_to_float(regs, target.word_order, target.byte_order):.1f}"
        lines.append(f"{pt.from_:<18}{si_txt:>14}   {pt.addr:>9}{raw_txt:>14}")
    return "\n".join(lines)


class SampleStats:
    """Per-point spread across a diag session.

    A single instantaneous reading cannot answer "is this value jittering?", and
    watching a refreshing screen is a bad instrument -- the eye reports the same
    noise differently at 1s and at 0.3s. This accumulates min/max and counts sign
    changes so two poll rates can be compared as numbers.
    """

    def __init__(self, points: tuple[str, ...]) -> None:
        self._points = points
        self._stats: dict[str, dict[str, float]] = {}
        self.samples = 0

    def record(self, values: dict[str, float]) -> None:
        self.samples += 1
        for name in self._points:
            value = values.get(name)
            if value is None or not math.isfinite(value):
                continue
            stat = self._stats.get(name)
            if stat is None:
                self._stats[name] = {
                    "min": value, "max": value, "last": value,
                    "last_signed": value, "sign_changes": 0,
                }
                continue
            # Compared against the last *signed* sample, not simply the previous
            # one: a value that walks 5 -> 0 -> -5 crossed zero once, and
            # comparing with the 0.0 in the middle would score that as none.
            if value * stat["last_signed"] < 0:
                stat["sign_changes"] += 1
            if value != 0.0:
                stat["last_signed"] = value
            stat["min"] = min(stat["min"], value)
            stat["max"] = max(stat["max"], value)
            stat["last"] = value

    def render(self, interval: float) -> str:
        lines = [
            "",
            f"spread over {self.samples} sample(s) at {interval:g}s "
            f"({self.samples * interval:.0f}s of history)",
            f"{'point':<12}{'last':>14}{'min':>14}{'max':>14}"
            f"{'peak-peak':>14}{'sign flips':>12}",
            "-" * 80,
        ]
        for name in self._points:
            stat = self._stats.get(name)
            if stat is None:
                lines.append(f"{name:<12}{'-':>14}")
                continue
            lines.append(
                f"{name:<12}{stat['last']:>14.3f}{stat['min']:>14.3f}"
                f"{stat['max']:>14.3f}{stat['max'] - stat['min']:>14.3f}"
                f"{int(stat['sign_changes']):>12}"
            )
        return "\n".join(lines)


def select_bridge(config: AppConfig, name: str | None = None) -> BridgeConf:
    """Resolve a --bridge name to its spec; default the first bridge.

    diag/selftest/static are single-bridge modes -- they print or serve one
    meter's worth of data, and interleaving two would make the output unreadable.
    The flag is what lets them target the SmartLogger bridge during bring-up.
    """
    specs = config.bridge_specs
    if name is None:
        return specs[0]
    for spec in specs:
        if spec.name == name:
            return spec
    available = ", ".join(s.name for s in specs)
    raise SystemExit(f"unknown bridge {name!r} (configured: {available})")


def run_diag_command(args) -> int:
    config = load_config(args.config)
    registers = load_registers(args.registers)
    spec = select_bridge(config, getattr(args, "bridge", None))
    if args.command == "selftest":
        return _run_selftest(config, registers, spec)
    return _run_diag(registers, spec, interval=getattr(args, "interval", 1.0))


def _synthetic_values(registers: RegisterMap) -> dict[str, float]:
    demo = {"u_l1": 230.0, "u_l2": 231.0, "u_l3": 229.0, "i_l1": 5.0, "i_l2": 5.1, "i_l3": 4.9,
            "p_total": 1500.0, "q_total": 200.0, "pf_total": 0.95, "freq": 50.0,
            "s_l1": 1150.0, "s_l2": 1178.1, "s_l3": 1122.1, "s_total": 3450.2,
            "imp_energy_total": 1234.5, "exp_energy_total": 67.8}
    # Zero-fill from every canonical name any output map reads, so the bench sees
    # a complete image regardless of which source section a bridge uses.
    required = {pt.from_ for _name, side in registers.targets for pt in side.points.values()}
    values = {k: demo.get(k, 0.0) for k in required}
    # Net energy is normally computed by the poller; without it the mbpoll bench
    # would show the net_* DTSU registers stuck at zero.
    compute_derived(values)
    return values


def _run_selftest(config, registers, spec: BridgeConf) -> int:
    from .dtsu_server import build_context, supervise_server, update_datastore

    async def _main() -> None:
        store = CanonicalStore()
        gate = HealthGate(spec.safety.max_data_age_s)
        targets = [side for _name, side in registers.targets]
        context = build_context(
            targets,
            spec.dtsu.slave_id,
            dtsu_cfg=spec.dtsu,
            sigen_identity=registers.dtsu_sigen_identity,
        )
        values = _synthetic_values(registers)
        stop = asyncio.Event()

        async def _feeder() -> None:
            while not stop.is_set():
                store.update(values, asyncio.get_running_loop().time())
                update_datastore(
                    context, spec.dtsu.slave_id, values, targets,
                    ct_ratio=spec.dtsu.identity.ir_at,
                )
                await asyncio.sleep(0.5)

        print(
            f"selftest: serving synthetic DTSU data for bridge {spec.name!r} over "
            f"{spec.dtsu.transport} (see config/config.json); bench with mbpoll. "
            "Ctrl-C to stop."
        )
        await asyncio.gather(
            _feeder(),
            supervise_server(spec.dtsu, context, store, gate,
                             spec.safety.check_interval_s, stop),
        )

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


# What a bring-up actually stares at: the signed flows plus the two inputs they
# are computed from, so an unstable P can be traced to U, I or neither.
WATCHED_POINTS = ("p_total", "q_total", "pf_total", "u_l1", "i_l1", "freq")


def _run_diag(registers, spec: BridgeConf, interval: float = 1.0) -> int:
    """Poll one bridge's source and print the decoded table. Serves nothing."""
    from pymodbus.client import AsyncModbusTcpClient

    from .app import _POLL_ONCE

    source_side = registers.source_by_name(spec.source.register_map)
    if spec.source.type == "nd45":
        from .nd45_poller import validate_source_coverage
    else:
        from .huawei_poller import validate_source_coverage
    validate_source_coverage(source_side)
    poll_once = _POLL_ONCE[spec.source.type]

    async def _main() -> None:
        client = AsyncModbusTcpClient(spec.source.host, port=spec.source.port,
                                      timeout=spec.source.timeout_s)
        await client.connect()
        stats = SampleStats(WATCHED_POINTS)
        try:
            while True:
                t0 = time.monotonic()
                try:
                    values = await poll_once(client, source_side, spec.source.unit_id)
                    age = time.monotonic() - t0
                    healthy = True
                except Exception as exc:  # noqa: BLE001
                    values, age, healthy = {}, float("inf"), False
                    print(f"poll error: {exc}")
                stats.record(values)
                print("\033[2J\033[H", end="")  # clear screen
                print(f"bridge {spec.name!r}  source {spec.source.type} "
                      f"@ {spec.source.host}:{spec.source.port} unit {spec.source.unit_id}"
                      f"   polling every {interval:g}s "
                      f"(the bridge itself uses {spec.source.poll_interval_s:g}s)")
                print(
                    render_table(
                        source_side,
                        registers.dtsu_target,
                        values,
                        age,
                        healthy,
                        ct_ratio=spec.dtsu.identity.ir_at,
                    )
                )
                print(stats.render(interval))
                await asyncio.sleep(interval)
        finally:
            client.close()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0
