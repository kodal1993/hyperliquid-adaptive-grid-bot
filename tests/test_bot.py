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
    df = pd.DataFrame({"close": [100 + i for i in range(40)], "high": [101 + i for i in range(40)], "low": [99 + i for i in range(40)]})
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
