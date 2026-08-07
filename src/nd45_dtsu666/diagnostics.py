"""Diagnostic table renderer + diag/selftest command runners."""

from __future__ import annotations

import asyncio
import time

from .canonical import CanonicalStore, HealthGate, SampleStats, compute_derived
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
    from .app import _make_client_factory, _POLL_ONCE, _VALIDATE_COVERAGE
    from .config import EtangoSourceConf

    source_side = registers.source_by_name(spec.source.register_map)
    _VALIDATE_COVERAGE[spec.source.type](source_side)
    poll_once = _POLL_ONCE[spec.source.type]

    if isinstance(spec.source, EtangoSourceConf):
        where = "@ " + ", ".join(
            f"{d.host}:{d.port}/{d.unit_id}" for d in spec.source.devices
        )
    else:
        where = f"@ {spec.source.host}:{spec.source.port} unit {spec.source.unit_id}"

    async def _main() -> None:
        client = _make_client_factory(spec)()
        await client.connect()
        stats = SampleStats(WATCHED_POINTS, window=600)
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
                      f"{where}"
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
