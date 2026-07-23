"""Diagnostic table renderer + diag/selftest command runners."""

from __future__ import annotations

import asyncio
import time

from .canonical import CanonicalStore, HealthGate
from .codec import registers_to_float
from .config import RegisterMap, load_config, load_registers
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


def run_diag_command(args) -> int:
    config = load_config(args.config)
    registers = load_registers(args.registers)
    if args.command == "selftest":
        return _run_selftest(config, registers)
    return _run_diag(config, registers)


def _synthetic_values(registers: RegisterMap) -> dict[str, float]:
    demo = {"u_l1": 230.0, "u_l2": 231.0, "u_l3": 229.0, "i_l1": 5.0, "i_l2": 5.1, "i_l3": 4.9,
            "p_total": 1500.0, "q_total": 200.0, "pf_total": 0.95, "freq": 50.0,
            "s_l1": 1150.0, "s_l2": 1178.1, "s_l3": 1122.1, "s_total": 3450.2,
            "imp_energy_total": 1234.5, "exp_energy_total": 67.8}
    values = {k: demo.get(k, 0.0) for k in registers.nd45_source.points}
    # Net energy is normally computed by nd45_poller.poll_once; without it the
    # mbpoll bench would show the net_* DTSU registers stuck at zero.
    from .nd45_poller import compute_derived

    compute_derived(values)
    return values


def _run_selftest(config, registers) -> int:
    from .dtsu_server import build_context, supervise_server, update_datastore

    async def _main() -> None:
        store = CanonicalStore()
        gate = HealthGate(config.safety.max_data_age_s)
        targets = [
            registers.dtsu_target,
            registers.dtsu_sigen_ext_target,
            registers.dtsu_sigen_ext_energy,
        ]
        context = build_context(
            targets,
            config.dtsu.slave_id,
            dtsu_cfg=config.dtsu,
            sigen_identity=registers.dtsu_sigen_identity,
        )
        values = _synthetic_values(registers)
        stop = asyncio.Event()

        async def _feeder() -> None:
            while not stop.is_set():
                store.update(values, asyncio.get_running_loop().time())
                update_datastore(
                    context, config.dtsu.slave_id, values, targets,
                    ct_ratio=config.dtsu.identity.ir_at,
                )
                await asyncio.sleep(0.5)

        print(
            f"selftest: serving synthetic DTSU data over {config.dtsu.transport} "
            "(see config/config.json); bench with mbpoll. Ctrl-C to stop."
        )
        await asyncio.gather(
            _feeder(),
            supervise_server(config.dtsu, context, store, gate,
                             config.safety.check_interval_s, stop),
        )

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def _run_diag(config, registers) -> int:
    from pymodbus.client import AsyncModbusTcpClient

    from .nd45_poller import poll_once, validate_source_coverage

    validate_source_coverage(registers.nd45_source)

    async def _main() -> None:
        client = AsyncModbusTcpClient(config.nd45.host, port=config.nd45.port,
                                      timeout=config.nd45.timeout_s)
        await client.connect()
        try:
            while True:
                t0 = time.monotonic()
                try:
                    values = await poll_once(client, registers.nd45_source, config.nd45.unit_id)
                    age = time.monotonic() - t0
                    healthy = True
                except Exception as exc:  # noqa: BLE001
                    values, age, healthy = {}, float("inf"), False
                    print(f"poll error: {exc}")
                print("\033[2J\033[H", end="")  # clear screen
                print(
                    render_table(
                        registers.nd45_source,
                        registers.dtsu_target,
                        values,
                        age,
                        healthy,
                        ct_ratio=config.dtsu.identity.ir_at,
                    )
                )
                await asyncio.sleep(1.0)
        finally:
            client.close()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0
