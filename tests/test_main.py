import argparse

from nd45_dtsu666 import __main__ as cli_main


def _args():
    return argparse.Namespace(config="config/config.json", registers="config/registers.json")


def test_cmd_run_swallows_keyboard_interrupt(monkeypatch):
    def raise_interrupt(coro):
        coro.close()  # never-started coroutine must be closed to avoid a warning
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main.asyncio, "run", raise_interrupt)

    # Ctrl-C on a platform where _install_signal_handlers can't attach (e.g.
    # Windows, per its own NotImplementedError guard) surfaces as a real
    # KeyboardInterrupt inside asyncio.run -- `run` must exit cleanly like
    # `monitor` already does, not crash with a raw traceback.
    assert cli_main._cmd_run(_args()) == 0


def test_cmd_static_swallows_keyboard_interrupt(monkeypatch):
    def raise_interrupt(coro):
        coro.close()
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_main.asyncio, "run", raise_interrupt)

    assert cli_main._cmd_static(_args()) == 0


def test_main_dispatches_static_command(monkeypatch):
    called = []

    def fake_static(args):
        called.append(args.command)
        return 17

    monkeypatch.setattr(cli_main, "_cmd_static", fake_static, raising=False)

    assert cli_main.main(["static"]) == 17
    assert called == ["static"]
