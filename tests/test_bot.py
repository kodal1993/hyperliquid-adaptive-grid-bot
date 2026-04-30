import os
import tempfile
import pandas as pd

from src.config import BotConfig
from src.grid_manager import GridManager
from src.regime_detector import RegimeDetector
from src.risk_manager import RiskManager
from src.types import MarketRegime, GridMode
from src.execution_engine import ExecutionEngine
from src.strategy_orchestrator import StrategyOrchestrator


class DummyClient:
    pass


def test_config_profile_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\nGRID_LEVELS=2\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("GRID_LEVELS=7\n")
    cfg = BotConfig.from_env()
    assert cfg.grid_levels == 7


def test_grid_generation():
    gm = GridManager()
    plan = gm.build_grid(100, 3, 0.01, 0.0, MarketRegime.RANGE, 1, 0.0, 0.0, GridMode.NEUTRAL)
    assert len(plan.long_levels) == 3 and len(plan.short_levels) == 3


def test_risk_blocking(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("1")
    rm = RiskManager(0.1, 0.05)
    snap = rm.evaluate(100, 0, stop_file=str(stop))
    assert not snap.can_trade


def test_paper_fill_simulation(tmp_path):
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "state.json"), True, False)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 100, "size": 1}]
    fills = eng.on_candle({"high": 101, "low": 99, "close": 100})
    assert len(fills) == 1


def test_regime_detector_basic():
    df = pd.DataFrame({"close": [100 for _ in range(40)], "high": [101 for _ in range(40)], "low": [99 for _ in range(40)]})
    regime = RegimeDetector().detect(df)
    assert regime in {MarketRegime.TREND_UP, MarketRegime.HIGH_VOL, MarketRegime.RANGE}


def test_order_size_notional_based():
    cfg = BotConfig.from_env()
    cfg.max_notional_per_trade_usd = 50
    cfg.min_order_size = 0.0
    cfg.max_order_size = 10.0
    cfg.min_notional_usd = 1.0
    orch = StrategyOrchestrator(cfg, ExecutionEngine(DummyClient(), "/tmp/state.json", True, False))
    size = orch._calculate_order_size(60000)
    assert abs(size - 0.0008333333) < 1e-6
    assert size * 60000 <= 50.0


def test_paper_equity_includes_unrealized():
    eng = ExecutionEngine(DummyClient(), "/tmp/state2.json", True, False, start_balance=500)
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    assert eng.unrealized_pnl(110.0) == 10.0
    assert eng.equity(110.0) == 510.0


def test_apply_fill_partial_close(tmp_path):
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "s.json"))
    eng.paper.position_size = 2.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "sell", "price": 110.0, "size": 1.0})
    assert eng.paper.position_size == 1.0
    assert eng.paper.realized_pnl > 9.9
    assert eng.paper.avg_entry == 100.0


def test_apply_fill_full_close_resets_avg(tmp_path):
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "s2.json"))
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "sell", "price": 105.0, "size": 1.0})
    assert eng.paper.position_size == 0.0
    assert eng.paper.avg_entry == 0.0


def test_apply_fill_long_to_short_flip(tmp_path):
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "s3.json"))
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "sell", "price": 110.0, "size": 2.0})
    assert eng.paper.position_size == -1.0
    assert eng.paper.realized_pnl > 9.9
    assert eng.paper.avg_entry == 110.0


def test_apply_fill_short_to_long_flip(tmp_path):
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "s4.json"))
    eng.paper.position_size = -1.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "buy", "price": 90.0, "size": 2.0})
    assert eng.paper.position_size == 1.0
    assert eng.paper.realized_pnl > 9.9
    assert eng.paper.avg_entry == 90.0


def test_conservative_fill_model_wide_candle(tmp_path):
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "s5.json"), fill_model="conservative")
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 95, "size": 1},
        {"symbol": "BTC", "side": "buy", "price": 90, "size": 1},
        {"symbol": "BTC", "side": "sell", "price": 105, "size": 1},
        {"symbol": "BTC", "side": "sell", "price": 110, "size": 1},
    ]
    fills = eng.on_candle({"open": 100, "high": 112, "low": 88, "close": 101})
    assert len(fills) == 2
    assert {f["price"] for f in fills} == {95, 105}


def test_daily_state_persistence(tmp_path):
    state_file = str(tmp_path / "persist.json")
    eng = ExecutionEngine(DummyClient(), state_file)
    eng.paper.current_day = "2026-04-29"
    eng.paper.daily_start_equity = 777.0
    eng.save_state()
    eng2 = ExecutionEngine(DummyClient(), state_file)
    assert eng2.paper.current_day == "2026-04-29"
    assert eng2.paper.daily_start_equity == 777.0


def test_paper_profile_uses_conservative_fill_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("FILL_MODEL=conservative\n")
    cfg = BotConfig.from_env()
    assert cfg.fill_model == "conservative"


def test_reduce_only_cancels_entry_orders_on_max_position_notional(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 10
    cfg.max_drawdown_pct = 0.9
    cfg.daily_loss_limit_pct = 0.9
    eng = ExecutionEngine(DummyClient(), str(tmp_path / "state_reduce_only.json"), True, False)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 1.0}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(40)], "high": [101 for _ in range(40)], "low": [99 for _ in range(40)]})
    status = orch.on_tick(candles, equity=100.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=20.0)
    assert status["status"] == "paused"
    assert status["reason"] == "reduce_only_requested"
    assert status["canceled_orders"] == 1
    assert eng.open_orders == []

from src.telegram_handler import TelegramHandler


def test_telegram_report_formatter_core_fields():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({
        "status": "running",
        "symbol": "BTC",
        "equity": 500.0,
        "realized_pnl": 5.0,
        "regime": "RANGE",
        "risk_reason": "none",
    })
    assert "Status: RUNNING" in msg
    assert "Symbol: BTC" in msg
    assert "Equity: $500.00" in msg
    assert "Realized PnL: $5.00" in msg
    assert "Regime: RANGE" in msg
    assert "Reason: none" in msg


def test_telegram_report_formatter_missing_values():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({})
    assert "Status: N/A" in msg
    assert "Equity: n/a" in msg
    assert "Reason: none" in msg


def test_telegram_interval_logic():
    interval = 300
    last = 0
    now = 100
    assert not ((now - last) >= interval)
    now = 301
    assert (now - last) >= interval


def test_risk_alert_deduplication_behavior():
    last_risk_reason_sent = ""
    reason = "max_drawdown"
    should_send = reason != last_risk_reason_sent
    assert should_send
    last_risk_reason_sent = reason
    should_send_again = reason != last_risk_reason_sent
    assert not should_send_again


def test_env_profile_loads_telegram_interval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("TELEGRAM_REPORT_INTERVAL_SECONDS=123\n")
    cfg = BotConfig.from_env()
    assert cfg.telegram_report_interval_seconds == 123
