"""CLI entrypoint: run | monitor[_nd45|_hsm] | rtudebug | static | diag | selftest."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .app import run_app
from .config import MqttConf, load_config, load_mqtt_config, load_registers

log = logging.getLogger(__name__)


def _load_config(args):
    """Load config.json and apply the metrics command-line overrides."""
    config = load_config(args.config)
    # getattr: callers other than main()'s parser (tests, diag) build args
    # namespaces without these optional flags.
    if getattr(args, "no_metrics", False):
        config.prometheus.enabled = False
    port = getattr(args, "metrics_port", None)
    if port is not None:
        config.prometheus.port = port
    return config


def _mqtt_path(args) -> str:
    """Where mqtt.json lives: `--mqtt`, else the sibling of `--config`.

    Defaulting to a sibling rather than a CWD-relative "config/mqtt.json" is what
    keeps the systemd unit unchanged: it passes an absolute --config, so the MQTT
    file is found next to it without a new ExecStart argument. That matters more
    than it looks -- on this host /etc is a tmpfs overlay (DEPLOY.md 0.4), so unit
    edits need overlayroot-chroot or they evaporate on reboot, and a --mqtt flag
    baked into the unit would break a symlink rollback to a checkout that predates
    this feature ("unrecognized arguments: --mqtt", bridge dead).
    """
    explicit = getattr(args, "mqtt", None)
    if explicit:
        return explicit
    return str(Path(args.config).with_name("mqtt.json"))


def _load_mqtt(args) -> MqttConf:
    """Load mqtt.json and apply the MQTT command-line overrides."""
    # getattr throughout, same contract as _load_config: tests and diagnostics
    # build bare Namespaces holding only config/registers.
    path = _mqtt_path(args)
    conf = load_mqtt_config(path)
    if conf is None:
        log.info("No MQTT config at %s; MQTT publishing disabled.", path)
        conf = MqttConf()
    if getattr(args, "no_mqtt", False):
        conf.enabled = False
    return conf


def _install_signal_handlers(loop, stop_event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows dev
            pass


def _cmd_run(args) -> int:
    """Run every enabled bridge, or just `--bridge <name>`.

    One bridge per invocation is how the deployment runs: a systemd instance each,
    so a crash or a restart of one cannot silence the other's RS-485. The whole
    config is still loaded, which keeps the cross-bridge validators (shared serial
    port, colliding ports) working even though each service runs one bridge.
    """
    config = _load_config(args)
    registers = load_registers(args.registers)
    only = getattr(args, "bridge", None)
    # `run` only. The monitor/rtudebug/static modes are diagnostics a human runs
    # on a device where the production service is usually also running: a second
    # client with the same id would evict it, and static's synthetic values would
    # be indistinguishable from real ones on a dashboard. Wiring MQTT into run_app
    # alone makes that structural rather than a rule someone has to remember.
    mqtt = _load_mqtt(args)

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
        await run_app(config, registers, stop_event, only=only, mqtt=mqtt)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_monitor(args, source_type: str | None = None) -> int:
    """Run every bridge; display all of them, or the one matching `source_type`.

    Every bridge always runs -- watching one must not change the other's timing or
    leave its output unserved. `source_type` only picks what is on screen.
    """
    config = _load_config(args)
    registers = load_registers(args.registers)
    from .monitor import run_monitor, select_bridge_by_source

    if source_type is not None:
        bridge_name = select_bridge_by_source(config, source_type)
    else:
        bridge_name = getattr(args, "bridge", None)

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
        await run_monitor(config, registers, stop_event, bridge_name=bridge_name)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_rtudebug(args) -> int:
    config = _load_config(args)
    registers = load_registers(args.registers)
    from .rtudebug import run_rtudebug

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
        await run_rtudebug(config, registers, stop_event, bridge_name=args.bridge)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_static(args) -> int:
    config = _load_config(args)
    registers = load_registers(args.registers)
    from .static_debug import run_static_debug

    async def _main() -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), stop_event)
        await run_static_debug(config, registers, stop_event, bridge_name=args.bridge)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nd45-dtsu666")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--registers", default="config/registers.json")
    parser.add_argument(
        "--metrics-port", type=int, default=None,
        help="override prometheus.port from config.json",
    )
    parser.add_argument(
        "--no-metrics", action="store_true", help="disable the Prometheus endpoint"
    )
    parser.add_argument(
        "--mqtt", default=None,
        help="MQTT publisher config (default: mqtt.json next to --config). "
             "A missing file means MQTT is disabled.",
    )
    parser.add_argument(
        "--no-mqtt", action="store_true",
        help="disable MQTT publishing regardless of mqtt.json",
    )
    parser.add_argument(
        "--bridge", default=None,
        help="bridge to target in the single-bridge modes "
             "(monitor/rtudebug/static/diag/selftest); defaults to the first configured "
             "bridge. Every bridge always runs; this only selects what is shown or traced.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run every enabled bridge, or one with --bridge")
    sub.add_parser(
        "monitor", help="run every bridge, showing a dashboard for each (or --bridge)"
    )
    sub.add_parser(
        "monitor_nd45", help="run every bridge, showing the ND45 bridge's dashboard"
    )
    sub.add_parser(
        "monitor_hsm",
        help="run every bridge, showing the Huawei SmartLogger bridge's dashboard",
    )
    sub.add_parser(
        "monitor_etango",
        help="run every bridge, showing the eTango bridge's dashboard",
    )
    sub.add_parser(
        "rtudebug", help="run every bridge, tracing register reads on one of them"
    )
    sub.add_parser(
        "static", help="serve stable JSON-configured values without connecting a source"
    )
    diag = sub.add_parser(
        "diag", help="poll one bridge's source and print the decoded table"
    )
    diag.add_argument(
        "--interval", type=float, default=1.0,
        help="seconds between polls (default 1.0). Set it to the bridge's own "
             "source.poll_interval_s to see the source exactly as the running "
             "bridge samples it -- a value that looks steady at 1s and jitters at "
             "0.3s is telling you something about the meter, not the bridge.",
    )
    sub.add_parser("selftest", help="serve synthetic data on one bridge for an mbpoll bench")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "monitor":
        return _cmd_monitor(args)
    if args.command == "monitor_nd45":
        return _cmd_monitor(args, source_type="nd45")
    if args.command == "monitor_hsm":
        return _cmd_monitor(args, source_type="huawei")
    if args.command == "monitor_etango":
        return _cmd_monitor(args, source_type="etango")
    if args.command == "rtudebug":
        return _cmd_rtudebug(args)
    if args.command == "static":
        return _cmd_static(args)
    if args.command in ("diag", "selftest"):
        from .diagnostics import run_diag_command

        return run_diag_command(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
