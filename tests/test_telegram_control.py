import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("tcb_under_test", PROJECT_ROOT / "scripts" / "telegram_control_bot.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["tcb_under_test"] = module  # so @dataclass can resolve annotations
    spec.loader.exec_module(module)
    return module


tcb = _load_module()


def _cfg():
    return SimpleNamespace(default_symbol="BTC")


def test_load_instances_default_single(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CONTROL_INSTANCES", raising=False)
    instances = tcb.load_instances(_cfg())
    assert len(instances) == 1
    assert instances[0].symbol == "BTC"


def test_load_instances_multi(monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_CONTROL_INSTANCES",
        "BTC:hyperliquid-grid-bot-live:/root/hyperliquid-adaptive-grid-bot-live:BTC,"
        "ETH:hyperliquid-grid-bot-eth:/root/hyperliquid-adaptive-grid-bot-eth:ETH",
    )
    instances = tcb.load_instances(_cfg())
    assert [i.label for i in instances] == ["BTC", "ETH"]
    eth = instances[1]
    assert eth.service == "hyperliquid-grid-bot-eth"
    assert eth.symbol == "ETH"
    assert str(eth.status_file).endswith("hyperliquid-adaptive-grid-bot-eth/data/status.json")
    assert str(eth.trades_file).endswith("hyperliquid-adaptive-grid-bot-eth/logs/trades.jsonl")
    assert str(eth.workdir) == "/root/hyperliquid-adaptive-grid-bot-eth"


def test_run_pull_restart_all_updates_each_instance(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_INSTANCES", "BTC:svc-btc:/a:BTC,ETH:svc-eth:/b:ETH")
    calls = []

    def fake_run_cmd(args, timeout=30):
        calls.append(list(args))
        return (0, "Already up to date.") if args[0] == "git" else (0, "active")

    monkeypatch.setattr(tcb, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(tcb.time, "sleep", lambda *_a, **_k: None)

    out = tcb.run_pull_restart_all(tcb.load_instances(_cfg()))

    assert "BTC" in out and "ETH" in out
    assert ["git", "-C", "/a", "pull", "--ff-only"] in calls
    assert ["git", "-C", "/b", "pull", "--ff-only"] in calls
    assert ["systemctl", "restart", "svc-btc"] in calls
    assert ["systemctl", "restart", "svc-eth"] in calls


def test_run_pull_restart_all_skips_restart_on_pull_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_INSTANCES", "BTC:svc-btc:/a:BTC")
    calls = []

    def fake_run_cmd(args, timeout=30):
        calls.append(list(args))
        return (1, "merge conflict") if args[0] == "git" else (0, "active")

    monkeypatch.setattr(tcb, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(tcb.time, "sleep", lambda *_a, **_k: None)

    out = tcb.run_pull_restart_all(tcb.load_instances(_cfg()))

    assert "NEM lett újraindítva" in out
    assert ["systemctl", "restart", "svc-btc"] not in calls


def test_restart_controller_self_noop_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_CONTROL_SELF_SERVICE", raising=False)
    assert tcb.restart_controller_self() is None


def test_restart_controller_self_restarts_configured_service(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_SELF_SERVICE", "hyperliquid-telegram-control")
    calls = []

    def fake_run_cmd(args, timeout=30):
        calls.append(list(args))
        return (0, "")

    monkeypatch.setattr(tcb, "run_cmd", fake_run_cmd)

    out = tcb.restart_controller_self()

    assert calls == [["systemctl", "restart", "--no-block", "hyperliquid-telegram-control"]]
    assert "hyperliquid-telegram-control" in out


def test_restart_controller_self_reports_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_SELF_SERVICE", "hyperliquid-telegram-control")

    def fake_run_cmd(args, timeout=30):
        return (1, "Unit not found.")

    monkeypatch.setattr(tcb, "run_cmd", fake_run_cmd)

    out = tcb.restart_controller_self()

    assert "sikertelen" in out
    assert "Unit not found." in out


def test_resolve_targets(monkeypatch):
    monkeypatch.setenv(
        "TELEGRAM_CONTROL_INSTANCES",
        "BTC:svc-btc:/a:BTC,ETH:svc-eth:/b:ETH",
    )
    cfg = _cfg()
    instances = tcb.load_instances(cfg)
    assert [i.label for i in tcb._resolve_targets(cfg, instances, "eth")] == ["ETH"]
    assert [i.label for i in tcb._resolve_targets(cfg, instances, "BTC")] == ["BTC"]
    assert [i.label for i in tcb._resolve_targets(cfg, instances, "")] == ["BTC", "ETH"]
    # unknown arg -> all (don't silently target the wrong one)
    assert [i.label for i in tcb._resolve_targets(cfg, instances, "sol")] == ["BTC", "ETH"]


def test_callback_to_command():
    assert tcb._callback_to_command("status") == "/status"
    assert tcb._callback_to_command("startbot:ETH") == "/startbot ETH"


def test_run_on_labels_each_instance(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_INSTANCES", "BTC:svc-btc:/a:BTC,ETH:svc-eth:/b:ETH")
    cfg = _cfg()
    instances = tcb.load_instances(cfg)
    out = tcb._run_on(cfg, instances, lambda c: f"sym={c.default_symbol}")
    assert "BTC" in out and "ETH" in out
    assert "sym=BTC" in out and "sym=ETH" in out


def test_multi_instance_menu_has_per_symbol_buttons(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONTROL_INSTANCES", "BTC:svc-btc:/a:BTC,ETH:svc-eth:/b:ETH")
    instances = tcb.load_instances(_cfg())
    kb = tcb.main_menu_keyboard(instances)
    callbacks = {b.get("callback_data") for row in kb["inline_keyboard"] for b in row}
    assert "confirm:stopbot:ETH" in callbacks
    assert "cmd:startbot:BTC" in callbacks
    assert "confirm:closeposition:ETH" in callbacks
