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
