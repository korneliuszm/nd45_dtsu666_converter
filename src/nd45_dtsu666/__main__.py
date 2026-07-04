"""CLI entrypoint: run | monitor | diag | selftest."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .app import run_app
from .config import load_config, load_registers


def _install_signal_handlers(loop, stop_event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows dev
            pass


def _cmd_run(args) -> int:
    config = load_config(args.config)
    registers = load_registers(args.registers)

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
        await run_app(config, registers, stop_event)

    asyncio.run(_main())
    return 0


def _cmd_monitor(args) -> int:
    config = load_config(args.config)
    registers = load_registers(args.registers)
    from .monitor import run_monitor

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
        await run_monitor(config, registers, stop_event)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nd45-dtsu666")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--registers", default="config/registers.json")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the bridge")
    sub.add_parser("monitor", help="run the bridge with a live commissioning dashboard")
    sub.add_parser("diag", help="diagnostic table (Task 9)")
    sub.add_parser("selftest", help="serve synthetic data (Task 9)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "monitor":
        return _cmd_monitor(args)
    if args.command in ("diag", "selftest"):
        from .diagnostics import run_diag_command

        return run_diag_command(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
