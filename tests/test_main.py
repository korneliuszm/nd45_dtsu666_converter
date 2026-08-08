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


# --- mqtt ---


def test_load_mqtt_with_a_bare_namespace_finds_the_sibling_of_config():
    # The getattr contract: diag and several tests build a Namespace holding only
    # config/registers, and adding CLI flags must not break them.
    conf = cli_main._load_mqtt(_args())
    assert conf.enabled is False  # the shipped config/mqtt.json ships disabled
    assert conf.points == ["p_total"]


def test_mqtt_path_defaults_to_the_sibling_of_config_not_the_cwd():
    # This is what lets the systemd unit stay unchanged: it passes an absolute
    # --config, so mqtt.json is found beside it with no new ExecStart argument.
    args = argparse.Namespace(config="/opt/app/config/config.json")
    assert cli_main._mqtt_path(args) == "/opt/app/config/mqtt.json"


def test_mqtt_path_flag_wins_over_the_default():
    args = argparse.Namespace(config="config/config.json", mqtt="/etc/other.json")
    assert cli_main._mqtt_path(args) == "/etc/other.json"


def test_a_missing_mqtt_file_disables_publishing_without_failing(tmp_path, caplog):
    args = argparse.Namespace(config=str(tmp_path / "config.json"))
    with caplog.at_level("INFO"):
        conf = cli_main._load_mqtt(args)
    assert conf.enabled is False
    assert any("MQTT publishing disabled" in r.message for r in caplog.records)


def test_no_mqtt_flag_disables_an_enabled_file(tmp_path):
    path = tmp_path / "mqtt.json"
    path.write_text('{"enabled": true}', encoding="utf-8")
    args = argparse.Namespace(config="config/config.json", mqtt=str(path))
    assert cli_main._load_mqtt(args).enabled is True
    args.no_mqtt = True
    assert cli_main._load_mqtt(args).enabled is False


def test_cmd_run_hands_the_mqtt_conf_to_run_app(monkeypatch):
    seen = {}

    async def fake_run_app(config, registers, stop_event, only=None, mqtt=None):
        seen["mqtt"] = mqtt
        seen["only"] = only

    monkeypatch.setattr(cli_main, "run_app", fake_run_app)
    assert cli_main._cmd_run(_args()) == 0
    assert seen["mqtt"] is not None
    assert seen["mqtt"].points == ["p_total"]


def test_the_mqtt_flags_are_global_and_parse_before_the_subcommand(monkeypatch):
    called = {}

    def fake_run(args):
        called["mqtt"] = args.mqtt
        called["no_mqtt"] = args.no_mqtt
        return 0

    monkeypatch.setattr(cli_main, "_cmd_run", fake_run)
    assert cli_main.main(["--mqtt", "/tmp/x.json", "--no-mqtt", "run"]) == 0
    assert called == {"mqtt": "/tmp/x.json", "no_mqtt": True}
