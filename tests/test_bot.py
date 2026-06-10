import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path

import pandas as pd
import pytest
import requests

from src.config import BotConfig
from src.grid_manager import GridManager
from src.hyperliquid_client import HyperliquidClient, normalize_price, normalize_size
from src.regime_detector import RegimeDetector, RegimeSignal
from src.risk_manager import RiskManager
from src.types import MarketRegime, GridMode
from src.execution_engine import ExecutionEngine, LiveExecutionEngine, PaperExecutionEngine, would_increase_exposure
from src.strategy_orchestrator import StrategyOrchestrator
from src.startup_validation import validate_live_execution_gate


class DummyClient:
    pass


def make_test_engine(tmp_path, state_name: str, **kwargs):
    return ExecutionEngine(
        DummyClient(),
        str(tmp_path / state_name),
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk_decisions.csv"),
        **kwargs,
    )


def test_config_profile_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\nGRID_LEVELS=2\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("GRID_LEVELS=7\n")
    cfg = BotConfig.from_env()
    assert cfg.grid_levels == 2




def test_env_profile_loaded_from_dotenv_before_profile_resolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENV_PROFILE", raising=False)
    monkeypatch.delenv("GRID_LEVELS", raising=False)
    (tmp_path / ".env").write_text("ENV_PROFILE=conservative\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "conservative.env").write_text("GRID_LEVELS=4\n")

    cfg = BotConfig.from_env()

    assert cfg.env_profile == "conservative"
    assert cfg.grid_levels == 4


def test_os_env_profile_precedence_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENV_PROFILE", "paper")
    monkeypatch.delenv("GRID_LEVELS", raising=False)
    (tmp_path / ".env").write_text("ENV_PROFILE=conservative\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("GRID_LEVELS=11\n")
    (tmp_path / "config" / "conservative.env").write_text("GRID_LEVELS=4\n")

    cfg = BotConfig.from_env()

    assert cfg.env_profile == "paper"
    assert cfg.grid_levels == 11

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


def test_risk_allows_exposure_reducing_trade_even_when_directional_cap_exceeded():
    rm = RiskManager(0.1, 0.05)
    snap = rm.evaluate(
        equity=1000,
        daily_pnl_pct=0,
        one_direction_exposure_pct=0.9,
        max_one_direction_exposure_pct=0.6,
        attempted_side="buy",
        would_increase_exposure=False,
    )
    assert snap.can_trade
    assert snap.decision == "allow"
    assert snap.exposure_reducing_override is True


def test_paper_fill_simulation(tmp_path):
    eng = make_test_engine(tmp_path, "state.json", paper_mode=True, enable_live_trading=False)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 100, "size": 1}]
    fills = eng.on_candle({"high": 101, "low": 99, "close": 100})
    assert len(fills) == 1


def test_regime_detector_basic():
    df = pd.DataFrame({"close": [100 for _ in range(40)], "high": [101 for _ in range(40)], "low": [99 for _ in range(40)]})
    regime = RegimeDetector().detect(df)
    assert regime in {MarketRegime.TREND_UP, MarketRegime.HIGH_VOL, MarketRegime.RANGE}


def test_order_size_notional_based(tmp_path):
    cfg = BotConfig.from_env()
    cfg.order_notional_usd = 50
    cfg.max_notional_per_trade_usd = 50
    cfg.min_order_size = 0.0
    cfg.max_order_size = 10.0
    cfg.min_notional_usd = 1.0
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "state.json", paper_mode=True, enable_live_trading=False))
    size = orch._calculate_order_size(60000)
    assert abs(size - 0.0008333333) < 1e-6
    assert size * 60000 <= 50.0


def test_paper_equity_includes_unrealized(tmp_path):
    eng = make_test_engine(tmp_path, "state2.json", paper_mode=True, enable_live_trading=False, start_balance=500)
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    assert eng.unrealized_pnl(110.0) == 10.0
    assert eng.equity(110.0) == 510.0


def test_apply_fill_partial_close(tmp_path):
    eng = make_test_engine(tmp_path, "s.json")
    eng.paper.position_size = 2.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "sell", "price": 110.0, "size": 1.0})
    assert eng.paper.position_size == 1.0
    assert eng.paper.realized_pnl > 9.9
    assert eng.paper.avg_entry == 100.0


def test_apply_fill_full_close_resets_avg(tmp_path):
    eng = make_test_engine(tmp_path, "s2.json")
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "sell", "price": 105.0, "size": 1.0})
    assert eng.state.position_size == 0.0
    assert eng.paper.avg_entry == 0.0


def test_apply_fill_long_to_short_flip(tmp_path):
    eng = make_test_engine(tmp_path, "s3.json")
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "sell", "price": 110.0, "size": 2.0})
    assert eng.paper.position_size == -1.0
    assert eng.paper.realized_pnl > 9.9
    assert eng.paper.avg_entry == 110.0


def test_apply_fill_short_to_long_flip(tmp_path):
    eng = make_test_engine(tmp_path, "s4.json")
    eng.paper.position_size = -1.0
    eng.paper.avg_entry = 100.0
    eng._apply_fill({"side": "buy", "price": 90.0, "size": 2.0})
    assert eng.paper.position_size == 1.0
    assert eng.paper.realized_pnl > 9.9
    assert eng.paper.avg_entry == 90.0



def test_apply_fill_records_flip_trade_analytics(tmp_path):
    eng = make_test_engine(tmp_path, "flip_analytics.json")
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0

    eng._apply_fill({"symbol": "BTC", "side": "sell", "price": 110.0, "size": 2.0})

    [entry] = eng.consume_trade_log()
    assert entry["exposure_action"] == "flipping"
    assert entry["trade_category"] == "flip"
    assert entry["is_flip_trade"] is True
    assert entry["flip_trade_count"] == 1
    assert eng.flip_trade_count == 1
    assert eng.last_flip_trade_ts is not None


def test_regime_confirmation_and_confidence_checks(tmp_path):
    cfg = BotConfig.from_env()
    cfg.regime_confirmation_bars = 2
    cfg.regime_min_confidence = 0.7
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "regime_confirm.json", paper_mode=True, enable_live_trading=False))

    low_conf = orch._confirmed_regime(MarketRegime.TREND_UP, 0.6)
    first_high_conf = orch._confirmed_regime(MarketRegime.TREND_UP, 0.8)
    second_high_conf = orch._confirmed_regime(MarketRegime.TREND_UP, 0.8)

    assert low_conf == MarketRegime.RANGE
    assert first_high_conf == MarketRegime.RANGE
    assert second_high_conf == MarketRegime.TREND_UP


def test_conservative_fill_model_wide_candle(tmp_path):
    eng = make_test_engine(tmp_path, "s5.json", fill_model="conservative")
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 95, "size": 1},
        {"symbol": "BTC", "side": "buy", "price": 90, "size": 1},
        {"symbol": "BTC", "side": "sell", "price": 105, "size": 1},
        {"symbol": "BTC", "side": "sell", "price": 110, "size": 1},
    ]
    fills = eng.on_candle({"open": 100, "high": 112, "low": 88, "close": 101})
    assert len(fills) == 2
    assert {f["price"] for f in fills} == {95, 105}


def test_emergency_reduce_override_executes_all_exposure_reducing_touched_orders_in_paper_mode(tmp_path):
    eng = make_test_engine(tmp_path, "s6.json", fill_model="conservative", max_position_notional_usd=500.0)
    eng.paper.position_size = 5.0
    eng.paper.avg_entry = 100.0
    eng.open_orders = [
        {"symbol": "BTC", "side": "sell", "price": 101, "size": 1},  # exposure reducing
        {"symbol": "BTC", "side": "sell", "price": 102, "size": 1},  # exposure reducing
        {"symbol": "BTC", "side": "buy", "price": 99, "size": 1},   # exposure increasing
    ]
    fills = eng.on_candle({"open": 100, "high": 103, "low": 98, "close": 100})
    assert {f["price"] for f in fills} == {101, 102}


def test_emergency_reduce_override_does_not_apply_to_exposure_increasing_orders(tmp_path):
    eng = make_test_engine(tmp_path, "s7.json", fill_model="conservative", max_position_notional_usd=500.0)
    eng.paper.position_size = 5.0
    eng.paper.avg_entry = 100.0
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 99, "size": 1},
        {"symbol": "BTC", "side": "buy", "price": 98, "size": 1},
    ]
    fills = eng.on_candle({"open": 100, "high": 101, "low": 97, "close": 100})
    assert len(fills) == 0


def test_daily_state_persistence(tmp_path):
    state_file = str(tmp_path / "persist.json")
    eng = ExecutionEngine(DummyClient(), state_file, trade_ledger_csv=str(tmp_path / "trades.csv"), trade_ledger_jsonl=str(tmp_path / "trades.jsonl"), risk_decisions_csv=str(tmp_path / "risk_decisions.csv"))
    eng.paper.current_day = "2026-04-29"
    eng.paper.daily_start_equity = 777.0
    eng.save_state()
    eng2 = ExecutionEngine(DummyClient(), state_file, trade_ledger_csv=str(tmp_path / "trades.csv"), trade_ledger_jsonl=str(tmp_path / "trades.jsonl"), risk_decisions_csv=str(tmp_path / "risk_decisions.csv"))
    assert eng2.paper.current_day == "2026-04-29"
    assert eng2.paper.daily_start_equity == 777.0


def test_offline_engine_uses_conservative_fill_model_when_requested(tmp_path):
    eng = make_test_engine(tmp_path, "conservative_fill_model.json", paper_mode=True, enable_live_trading=False, fill_model="conservative")
    assert eng.fill_model == "conservative"

def test_reduce_only_places_directional_orders_on_max_position_notional(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 10
    cfg.max_drawdown_pct = 0.9
    cfg.daily_loss_limit_pct = 0.9
    eng = make_test_engine(tmp_path, "state_reduce_only.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = -2.0
    eng.open_orders = [{"symbol": "BTC", "side": "sell", "price": 101.0, "size": 1.0}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(40)], "high": [101 for _ in range(40)], "low": [99 for _ in range(40)]})
    status = orch.on_tick(candles, equity=100.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=20.0)
    assert status["status"] == "paused"
    assert status["reason"] == "reduce_only_requested"
    assert status["canceled_orders"] == 1
    assert status["reduce_only_placed"] > 0
    assert all(o["side"] == "buy" for o in eng.open_orders)


def test_close_only_resize_long_flip_disabled(tmp_path):
    eng = make_test_engine(tmp_path, "flip1.json", paper_mode=True, allow_position_flip=False)
    eng.paper.position_size = 39.0 / 80000.0
    eng.paper.avg_entry = 80000.0
    eng.open_orders = [{"symbol": "BTC", "side": "sell", "price": 80000.0, "size": 50.0 / 80000.0}]
    fills = eng.on_candle({"open": 80000, "high": 81000, "low": 79000, "close": 80000})
    assert len(fills) == 1
    assert abs(fills[0]["size"] - (39.0 / 80000.0)) < 1e-12
    assert eng.state.position_size == 0.0


def test_close_only_resize_short_flip_disabled(tmp_path):
    eng = make_test_engine(tmp_path, "flip2.json", paper_mode=True, allow_position_flip=False)
    eng.paper.position_size = -(39.0 / 80000.0)
    eng.paper.avg_entry = 80000.0
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 80000.0, "size": 50.0 / 80000.0}]
    fills = eng.on_candle({"open": 80000, "high": 81000, "low": 79000, "close": 80000})
    assert len(fills) == 1
    assert abs(fills[0]["size"] - (39.0 / 80000.0)) < 1e-12
    assert eng.state.position_size == 0.0


def test_flip_enabled_can_flip_when_caps_allow(tmp_path):
    eng = make_test_engine(tmp_path, "flip3.json", paper_mode=True, allow_position_flip=True)
    eng.paper.position_size = 39.0 / 80000.0
    eng.paper.avg_entry = 80000.0
    eng.open_orders = [{"symbol": "BTC", "side": "sell", "price": 80000.0, "size": 50.0 / 80000.0}]
    fills = eng.on_candle({"open": 80000, "high": 81000, "low": 79000, "close": 80000})
    assert len(fills) == 1
    assert eng.paper.position_size < 0


def test_grid_recenter_triggered_by_no_fill_cycles(tmp_path):
    cfg = BotConfig.from_env()
    cfg.no_fill_cycles_before_recenter = 1
    eng = make_test_engine(tmp_path, "recenter.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    eng.no_fill_cycles = 2
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    assert status["status"] == "running"


def test_telegram_report_includes_last_trade_age_warning():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({"last_real_trade_age_hours": 2.5})
    assert "WARNING" in msg


def test_trend_up_neutral_long_blocks_new_buy_but_allows_sell_exit(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 500
    cfg.allow_long_biased = False
    cfg.allow_short_biased = False
    eng = make_test_engine(tmp_path, "trend_up_long.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 1.0
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=100.0)
    assert status["status"] == "managing_position"
    assert status["reason"] == "neutral_entries_blocked_in_trend"
    assert status["allow_buys"] is False
    assert status["allow_sells"] is True
    assert status["allowed_to_trade"] is False
    assert status["allowed_to_reduce"] is True


def test_trend_down_neutral_short_blocks_new_sell_but_allows_buy_exit(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 500
    cfg.allow_long_biased = False
    cfg.allow_short_biased = False
    eng = make_test_engine(tmp_path, "trend_down_short.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = -1.0
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [200 - i for i in range(80)], "high": [201 - i for i in range(80)], "low": [199 - i for i in range(80)]})
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=100.0)
    assert status["status"] == "managing_position"
    assert status["reason"] == "neutral_entries_blocked_in_trend"
    assert status["allow_sells"] is False
    assert status["allow_buys"] is True
    assert status["allowed_to_trade"] is False
    assert status["allowed_to_reduce"] is True



def test_trend_up_long_biased_long_position_places_sell_exit(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 500
    cfg.allow_long_biased = True
    cfg.allow_short_biased = True
    eng = make_test_engine(tmp_path, "trend_up_biased_long_exit.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 1.0
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=179.0)

    assert status["status"] == "running"
    assert status["mode"] == GridMode.LONG_BIASED.value
    assert status["allow_buys"] is False
    assert status["allow_sells"] is True
    assert len(eng.open_orders) > 0
    assert all(o["side"] == "sell" for o in eng.open_orders)


def test_trend_down_short_biased_short_position_places_buy_exit(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 500
    cfg.allow_long_biased = True
    cfg.allow_short_biased = True
    eng = make_test_engine(tmp_path, "trend_down_biased_short_exit.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = -1.0
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [200 - i for i in range(80)], "high": [201 - i for i in range(80)], "low": [199 - i for i in range(80)]})

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=121.0)

    assert status["status"] == "running"
    assert status["mode"] == GridMode.SHORT_BIASED.value
    assert status["allow_sells"] is False
    assert status["allow_buys"] is True
    assert len(eng.open_orders) > 0
    assert all(o["side"] == "buy" for o in eng.open_orders)


def test_trend_biased_flat_position_allows_only_trend_entry(tmp_path):
    cfg = BotConfig.from_env()
    cfg.allow_long_biased = True
    cfg.allow_short_biased = True

    up_engine = make_test_engine(tmp_path, "trend_up_flat_entry.json", paper_mode=True, enable_live_trading=False)
    up_orch = StrategyOrchestrator(cfg, up_engine)
    up_candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})
    up_status = up_orch.on_tick(up_candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert up_status["status"] == "running"
    assert up_status["mode"] == GridMode.LONG_BIASED.value
    assert up_status["allow_buys"] is True
    assert up_status["allow_sells"] is False
    assert len(up_engine.open_orders) > 0
    assert all(o["side"] == "buy" for o in up_engine.open_orders)

    down_engine = make_test_engine(tmp_path, "trend_down_flat_entry.json", paper_mode=True, enable_live_trading=False)
    down_orch = StrategyOrchestrator(cfg, down_engine)
    down_candles = pd.DataFrame({"close": [200 - i for i in range(80)], "high": [201 - i for i in range(80)], "low": [199 - i for i in range(80)]})
    down_status = down_orch.on_tick(down_candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert down_status["status"] == "running"
    assert down_status["mode"] == GridMode.SHORT_BIASED.value
    assert down_status["allow_sells"] is True
    assert down_status["allow_buys"] is False
    assert len(down_engine.open_orders) > 0
    assert all(o["side"] == "sell" for o in down_engine.open_orders)


def test_orphan_position_without_open_orders_forces_recenter_despite_cooldown(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 500
    cfg.allow_long_biased = True
    cfg.position_recenter_cooldown_seconds = 3600
    eng = make_test_engine(tmp_path, "orphan_position_recenter.json", paper_mode=True, enable_live_trading=False)
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})

    orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    eng.open_orders = []
    eng.paper.position_size = 1.0
    orch.last_recenter_ts = datetime.now(timezone.utc)

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=179.0)

    assert status["force_recenter"] is True
    assert status["orphan_position_no_orders"] is True
    assert status["recenter_blocked_by_cooldown"] is True
    assert len(eng.open_orders) > 0
    assert all(o["side"] == "sell" for o in eng.open_orders)

def test_trend_with_flat_position_blocks_neutral_grid_generation(tmp_path):
    cfg = BotConfig.from_env()
    cfg.allow_long_biased = False
    cfg.allow_short_biased = False
    eng = make_test_engine(tmp_path, "trend_flat.json", paper_mode=True, enable_live_trading=False)
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    assert status["status"] == "paused"
    assert status["reason"] == "neutral_blocked_in_trend"
    assert len(eng.open_orders) == 0


def test_write_status_creates_data_status_json(tmp_path, monkeypatch):
    from src import main
    monkeypatch.chdir(tmp_path)
    main._write_status({"status": "running", "timestamp": "2026-01-01T00:00:00+00:00"})
    assert (tmp_path / "data" / "status.json").exists()


def test_write_fatal_error_creates_log_and_crashed_status(tmp_path, monkeypatch):
    from src import main
    monkeypatch.chdir(tmp_path)
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        main._write_fatal_error(exc)
    fatal = (tmp_path / "logs" / "fatal_error.log").read_text(encoding="utf-8")
    status = (tmp_path / "data" / "status.json").read_text(encoding="utf-8")
    assert "RuntimeError" in fatal
    assert "\"status\": \"crashed\"" in status


def test_single_instance_lock_blocks_second_acquire(tmp_path, monkeypatch):
    from src import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HYPERLIQUID_BOT_LOCK_FILE", str(tmp_path / "bot.lock"))
    h1 = main._acquire_single_instance_lock()
    assert h1 is not None
    h2 = main._acquire_single_instance_lock()
    assert h2 is None

    h1.close()

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
    assert "Hyperliquid bot órás státusz" in msg
    assert "Symbol: BTC" in msg
    assert "Equity: $500.00" in msg
    assert "Realized PnL: $5.00" in msg
    assert "RANGE /" in msg


def test_telegram_report_formatter_missing_values():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({})
    assert "Hyperliquid bot órás státusz" in msg
    assert "Equity: n/a" in msg
    assert "Reason: none" in msg


def test_telegram_report_zero_trade_window_message():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({"trades_1h": 0, "buy_count_1h": 0, "sell_count_1h": 0})
    assert "BUY count: 0" in msg
    assert "SELL count: 0" in msg
    assert "Nincs új BUY/SELL fill az elmúlt 1 órában." in msg


def test_telegram_fill_alert_buy_and_sell_formats():
    tg = TelegramHandler("", "")
    base_trade = {
        "timestamp": "2026-05-08T12:23:00+00:00",
        "symbol": "BTC",
        "price": 80564.96,
        "qty": 0.00061875,
        "notional": 49.85,
        "fee": 0.0199,
        "realized_pnl_delta": 0.0750,
        "realized_pnl_total": 23.1943,
        "position_size": 0.00048742,
        "position_notional": 39.27,
        "equity": 521.89,
        "regime": "RANGE",
        "mode": "neutral",
        "risk_state": "OK",
        "pause_reason": "",
        "reason": "grid_fill",
    }
    buy_msg = tg.format_fill_alert({**base_trade, "side": "buy"}, mode_name="PAPER")
    sell_msg = tg.format_fill_alert({**base_trade, "side": "sell", "position_size": -0.0001}, mode_name="LIVE")
    assert buy_msg.startswith("🟢 BUY EXECUTED")
    assert "Mode: PAPER" in buy_msg
    assert "Side" not in buy_msg
    assert sell_msg.startswith("🔴 SELL EXECUTED")
    assert "Mode: LIVE" in sell_msg
    assert "Position after: SHORT" in sell_msg




def test_telegram_report_formatter_prediction_fields():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({
        "prediction_bias": "STRONG_LONG",
        "confidence_score": 0.72,
        "bullish_probability": 0.61,
        "bearish_probability": 0.14,
        "neutral_probability": 0.25,
    })
    assert "Prediction:" in msg
    assert "Prediction: LONG" in msg
    assert "Confidence: 0.72" in msg
    assert "Bull/Bear/Neutral: 0.61 / 0.14 / 0.25" in msg

def test_last_valid_trade_ignores_invalid_btc_prices(tmp_path):
    from src.main import _read_last_valid_trade
    p = tmp_path / "trades.csv"
    p.write_text("timestamp,symbol,side,price,qty,notional\n2026-05-08T12:00:00+00:00,BTC,buy,110,0.1,11\n2026-05-08T12:05:00+00:00,BTC,buy,80564.96,0.001,80.56\n", encoding="utf-8")
    row = _read_last_valid_trade(p, "BTC")
    assert float(row["price"]) == 80564.96


def test_write_last_100_real_trades_sanitizes_extra_delimiter_rows(tmp_path, caplog):
    from src.main import _write_last_100_real_trades

    trades_csv = tmp_path / "trades.csv"
    out_csv = tmp_path / "last_100_real_trades.csv"
    trades_csv.write_text(
        "timestamp,symbol,side,price,qty,notional\n"
        "2026-05-08T12:05:00+00:00,BTC,buy,80564.96,0.001,80.56,unexpected_extra_value\n",
        encoding="utf-8",
    )

    _write_last_100_real_trades(trades_csv, out_csv, "BTC")

    output = out_csv.read_text(encoding="utf-8")
    assert out_csv.exists()
    assert "None" not in output
    assert "unexpected_extra_value" not in output
    assert "80564.96" in output
    assert "malformed_trade_csv_row_skipped_or_sanitized" in caplog.text


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
    monkeypatch.delenv("TELEGRAM_REPORT_INTERVAL_SECONDS", raising=False)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("TELEGRAM_REPORT_INTERVAL_SECONDS=123\n")
    cfg = BotConfig.from_env()
    assert cfg.telegram_report_interval_seconds == 123


def test_offline_metrics_reflect_post_fill_state(tmp_path):
    from src.main import _account_metrics

    class DummyCfg:
        paper_mode = True

    eng = make_test_engine(tmp_path, "post_fill_state.json", paper_mode=True, enable_live_trading=False, start_balance=500.0)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 100.0, "size": 1.0}]
    latest_close = 100.0

    pre_state = _account_metrics(DummyCfg(), eng, DummyClient(), latest_close)
    assert pre_state.unrealized_pnl == 0.0
    assert pre_state.equity == 500.0
    assert pre_state.position_notional == 0.0

    fills = eng.on_candle({"open": 100.0, "high": 101.0, "low": 99.0, "close": latest_close})
    assert len(fills) == 1

    post_state = _account_metrics(DummyCfg(), eng, DummyClient(), latest_close)
    assert post_state.unrealized_pnl == 0.0
    assert post_state.equity < pre_state.equity
    assert post_state.position_notional == 100.0


def test_short_position_cannot_place_more_sell_expansion_orders(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 100
    eng = make_test_engine(tmp_path, "state_short_guard.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = -1.0
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=100.0)
    assert status["status"] == "running"
    assert all(o["side"] == "buy" for o in eng.open_orders)


def test_long_position_cannot_place_more_buy_expansion_orders(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 100
    eng = make_test_engine(tmp_path, "state_long_guard.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 1.0
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=100.0)
    assert status["status"] == "running"
    assert all(o["side"] == "sell" for o in eng.open_orders)


def test_upward_price_drift_classified_trend_up():
    closes = [100 + (i * 0.2) for i in range(120)]
    df = pd.DataFrame({"close": closes, "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes]})
    regime = RegimeDetector().detect(df, trend_lookback_candles=60, trend_move_threshold_pct=0.006, ema_slope_threshold_pct=0.003)
    assert regime == MarketRegime.TREND_UP



def test_regime_detector_early_trend_up_from_higher_highs_and_lows():
    first = [100.0 + (i * 0.01) for i in range(30)]
    second = [100.2 + (i * 0.002) for i in range(30)]
    closes = first + second
    highs = [101.0 for _ in first] + [101.6 for _ in second]
    lows = [99.0 for _ in first] + [99.7 for _ in second]
    df = pd.DataFrame({"close": closes, "high": highs, "low": lows})

    signal = RegimeDetector().detect_signal(
        df,
        trend_lookback_candles=60,
        trend_move_threshold_pct=0.02,
        ema_slope_threshold_pct=0.02,
        trend_strength_threshold=0.4,
    )

    assert signal.regime == MarketRegime.TREND_UP
    assert signal.trend_structure_score > 0
    assert signal.momentum_score > 0
    assert signal.trend_strength_score > 0.4


def test_regime_detector_early_trend_down_from_lower_highs_and_lows():
    first = [100.0 - (i * 0.01) for i in range(30)]
    second = [99.8 - (i * 0.002) for i in range(30)]
    closes = first + second
    highs = [101.0 for _ in first] + [100.3 for _ in second]
    lows = [99.0 for _ in first] + [98.4 for _ in second]
    df = pd.DataFrame({"close": closes, "high": highs, "low": lows})

    signal = RegimeDetector().detect_signal(
        df,
        trend_lookback_candles=60,
        trend_move_threshold_pct=0.02,
        ema_slope_threshold_pct=0.02,
        trend_strength_threshold=0.4,
    )

    assert signal.regime == MarketRegime.TREND_DOWN
    assert signal.trend_structure_score < 0
    assert signal.momentum_score < 0
    assert signal.trend_strength_score > 0.4



def test_regime_detector_transition_score_accelerating_uptrend():
    closes = [100.0 if i < 40 else 100.0 + 0.01 * ((i - 39) ** 2) for i in range(80)]
    highs = [c * (1.001 + 0.00001 * i) for i, c in enumerate(closes)]
    lows = [c * (0.999 + 0.000005 * i) for i, c in enumerate(closes)]
    df = pd.DataFrame({"close": closes, "high": highs, "low": lows})

    signal = RegimeDetector().detect_signal(
        df,
        trend_lookback_candles=60,
        trend_move_threshold_pct=0.03,
        ema_slope_threshold_pct=0.03,
        trend_strength_threshold=0.8,
    )

    assert signal.regime == MarketRegime.TREND_UP
    assert signal.higher_high_sequence_score > 0
    assert signal.higher_low_sequence_score > 0
    assert signal.ema_slope_acceleration_score > 0
    assert signal.momentum_acceleration_score > 0
    assert signal.trend_transition_score > 0.7
    assert signal.transition_direction == "UP"
    assert signal.transition_confidence >= 0.7


def test_transition_confidence_accelerates_trend_confirmation(tmp_path):
    cfg = BotConfig.from_env()
    cfg.regime_confirmation_bars = 3
    cfg.regime_min_confidence = 0.95
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "transition_confirm.json", paper_mode=True, enable_live_trading=False))

    confirmed = orch._confirmed_regime(MarketRegime.TREND_UP, 0.75, transition_confidence=0.8)

    assert confirmed == MarketRegime.TREND_UP

def test_neutral_grid_blocked_when_directional_exposure_high(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_position_notional_usd = 100
    cfg.max_directional_exposure_pct = 0.65
    eng = make_test_engine(tmp_path, "state_dir_guard.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = -0.7  # $70 short at $100, above 65 threshold
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=70.0)
    assert status["status"] == "running"
    assert status["allow_sells"] is False


def test_would_increase_exposure_helper():
    assert would_increase_exposure(-0.005, "sell", 0.001) is True
    assert would_increase_exposure(-0.005, "buy", 0.001) is False
    assert would_increase_exposure(0.005, "buy", 0.001) is True
    assert would_increase_exposure(0.005, "sell", 0.001) is False
    assert would_increase_exposure(0.0, "buy", 0.001) is True
    assert would_increase_exposure(0.0, "sell", 0.001) is True


def test_risk_decision_long_reducing_vs_flipping(tmp_path):
    eng = make_test_engine(tmp_path, "risk_flip_long.json", max_position_notional_usd=500)
    eng.paper.position_size = 0.39
    decision, reason = eng._risk_decision({"symbol": "BTC", "side": "sell", "price": 100.0, "size": 0.20}, 100.0, "OK", "", "RANGE", "neutral")
    assert decision == "allow"
    assert reason == ""
    decision, reason = eng._risk_decision({"symbol": "BTC", "side": "sell", "price": 100.0, "size": 0.50}, 100.0, "OK", "", "RANGE", "neutral")
    assert decision == "block"
    assert reason == "position_flip_blocked"


def test_risk_decision_short_reducing_vs_flipping(tmp_path):
    eng = make_test_engine(tmp_path, "risk_flip_short.json", max_position_notional_usd=500)
    eng.paper.position_size = -0.39
    decision, reason = eng._risk_decision({"symbol": "BTC", "side": "buy", "price": 100.0, "size": 0.20}, 100.0, "OK", "", "RANGE", "neutral")
    assert decision == "allow"
    assert reason == ""
    decision, reason = eng._risk_decision({"symbol": "BTC", "side": "buy", "price": 100.0, "size": 0.50}, 100.0, "OK", "", "RANGE", "neutral")
    assert decision == "block"
    assert reason == "position_flip_blocked"


def test_neutral_blocked_in_trend_is_not_hard_risk_block():
    from src.main import _derive_risk_state
    assert _derive_risk_state("paused", "neutral_blocked_in_trend") == "STRATEGY_PAUSED"
    assert _derive_risk_state("paused", "emergency_stop") == "BLOCKED"


def test_exposure_reducing_allowed_even_with_blocked_risk_state(tmp_path):
    eng = make_test_engine(tmp_path, "risk_reduce_allowed.json", max_position_notional_usd=500)
    eng.paper.position_size = 0.39
    decision, reason = eng._risk_decision({"symbol": "BTC", "side": "sell", "price": 100.0, "size": 0.20}, 100.0, "BLOCKED", "max_position_notional", "RANGE", "neutral")
    assert decision == "allow"
    assert reason == ""


def test_exposure_gating_cases(tmp_path):
    eng = make_test_engine(tmp_path, "risk_cases.json", max_position_notional_usd=500, soft_exposure_cap_pct=0.6, hard_exposure_cap_pct=0.8, absolute_exposure_cap_pct=1.0)

    eng.paper.position_size = -0.005
    d,_ = eng._risk_decision({"symbol":"BTC","side":"sell","price":90000.0,"size":0.0006}, 90000.0, "OK", "", "range", "neutral")
    assert d == "block"
    d,_ = eng._risk_decision({"symbol":"BTC","side":"buy","price":90000.0,"size":0.0006}, 90000.0, "OK", "", "range", "neutral")
    assert d == "allow"

    eng.paper.position_size = 0.005
    d,_ = eng._risk_decision({"symbol":"BTC","side":"buy","price":90000.0,"size":0.0006}, 90000.0, "OK", "", "range", "neutral")
    assert d == "block"
    d,_ = eng._risk_decision({"symbol":"BTC","side":"sell","price":90000.0,"size":0.0006}, 90000.0, "OK", "", "range", "neutral")
    assert d == "allow"

    eng.paper.position_size = 0.0
    d,_ = eng._risk_decision({"symbol":"BTC","side":"buy","price":90000.0,"size":0.0001}, 90000.0, "OK", "", "range", "neutral")
    assert d == "allow"
    d,_ = eng._risk_decision({"symbol":"BTC","side":"sell","price":90000.0,"size":0.0001}, 90000.0, "OK", "", "range", "neutral")
    assert d == "allow"


def test_main_config_loads_dotenv_telegram_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_CHAT_ID=999\n")
    cfg = BotConfig.from_env()
    assert cfg.telegram_bot_token == "abc123"
    assert cfg.telegram_chat_id == "999"


def test_telegram_handler_gets_validated_config_values_from_dotenv(tmp_path, monkeypatch):
    from src.telegram_handler import TelegramHandler

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=test_token\nTELEGRAM_CHAT_ID=test_chat\n")
    cfg = BotConfig.from_env()
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)
    assert tg.token == "test_token"
    assert tg.chat_id == "test_chat"


def test_last_real_trade_ts_resolves_from_real_trade_csv(tmp_path, monkeypatch):
    from src.main import _resolve_last_real_trade_ts

    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "real_trades.csv").write_text(
        "timestamp,symbol,side,price,qty,notional\n"
        "2026-05-10T11:00:00+00:00,BTC,buy,101000,0.001,101\n",
        encoding="utf-8",
    )
    ts = _resolve_last_real_trade_ts("BTC")
    assert ts == "2026-05-10T11:00:00+00:00"


def test_send_startup_telegram_respects_disabled_flag():
    from src.main import _send_startup_telegram

    class Cfg:
        telegram_send_startup = False
        paper_mode = True
        hl_network = "testnet"
        default_symbol = "BTC"
        enable_live_trading = False
        env_profile = "paper"
        telegram_bot_token = "x"
        telegram_chat_id = "y"

    class DummyTg:
        def __init__(self):
            self.sent = False
            self.last_error = {}

        def send(self, text: str) -> bool:
            self.sent = True
            return True

    tg = DummyTg()
    status = _send_startup_telegram(Cfg(), tg)
    assert status == "disabled"
    assert tg.sent is False


def test_env_profile_does_not_override_dotenv_telegram_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\nTELEGRAM_BOT_TOKEN=abc123\nTELEGRAM_CHAT_ID=999\n", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", encoding="utf-8")
    cfg = BotConfig.from_env()
    assert cfg.telegram_bot_token == "abc123"
    assert cfg.telegram_chat_id == "999"


def test_dashboard_read_status_uses_official_data_path_only(tmp_path, monkeypatch):
    from src import dashboard_server

    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "status.json").write_text('{"status":"state"}', encoding="utf-8")
    (tmp_path / "data" / "status.json").write_text('{"status":"data"}', encoding="utf-8")

    status = dashboard_server.read_status()
    assert status["status"] == "data"

    (tmp_path / "data" / "status.json").unlink()
    status = dashboard_server.read_status()
    assert status == {}


def test_grid_recenter_anchor_updates_only_when_regrid():
    gm = GridManager()
    plan1 = gm.build_grid(100.0, 3, 0.003, 0.0, MarketRegime.RANGE, 1.0, 0.01, 0.0001, GridMode.NEUTRAL)
    assert plan1.should_regrid is True
    plan2 = gm.build_grid(100.5, 3, 0.003, 0.0, MarketRegime.RANGE, 1.0, 0.01, 0.0001, GridMode.NEUTRAL)
    assert plan2.should_regrid is False
    assert gm.last_mid == 100.0
    plan3 = gm.build_grid(101.1, 3, 0.003, 0.0, MarketRegime.RANGE, 1.0, 0.01, 0.0001, GridMode.NEUTRAL)
    assert plan3.should_regrid is True


def test_telegram_report_clarity_messages_and_fields():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({
        "strategy_status": "paused",
        "risk_state": "STRATEGY_PAUSED",
        "pause_reason": "neutral_blocked_in_trend",
        "last_block_reason": "neutral_blocked_in_trend",
        "allowed_to_trade": False,
        "trades_30m": 0,
        "next_order_side": "buy",
        "next_order_price": 100.0,
        "distance_to_buy_pct": 0.01,
        "distance_to_sell_pct": 0.02,
    })
    assert "strategy_status: paused" in msg
    assert "risk_state: STRATEGY_PAUSED" in msg
    assert "pause_reason: neutral_blocked_in_trend" in msg
    assert "allowed_to_trade: False" in msg
    assert "Trend piacban a neutral grid tiltva van, ezért nincs új order." in msg
    assert "next_order: BUY @ $100.00" in msg


def test_telegram_report_no_fill_reason_when_allowed():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({"allowed_to_trade": True, "trades_30m": 0})
    assert "Nincs fill, mert az ár nem érte el a következő grid szintet." in msg


def test_telegram_report_block_reason_when_not_allowed():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({"allowed_to_trade": False, "trades_30m": 1})
    assert "Risk/strategy block miatt nincs új pozícióépítés." in msg


def test_default_telegram_report_interval_is_3600(monkeypatch):
    monkeypatch.delenv("TELEGRAM_REPORT_INTERVAL_SECONDS", raising=False)
    cfg = BotConfig.from_env()
    assert cfg.telegram_report_interval_seconds == 3600


def test_neutral_blocked_in_trend_not_hard_risk_reason():
    from src.main import _derive_risk_state
    assert _derive_risk_state("paused", "neutral_blocked_in_trend") == "STRATEGY_PAUSED"


def test_format_status_report_uses_1h_fields():
    tg = TelegramHandler("", "")
    msg = tg.format_status_report({"trades_1h": 2, "buy_count_1h": 1, "sell_count_1h": 1, "realized_pnl_delta_1h": 3.0})
    assert "Utolsó 1 óra:" in msg
    assert "Trades: 2" in msg


def test_status_and_trades_command_minimum_format():
    status = {"risk_state": "OK", "pause_reason": "", "last_block_reason": "", "blocked_risk_decisions_1h": 0, "allowed_to_trade": True}
    risk_text = f"risk_state: {status.get('risk_state')}\npause_reason: {status.get('pause_reason') or 'none'}\nlast_block_reason: {status.get('last_block_reason') or 'none'}\nblocked_risk_decisions_1h: {status.get('blocked_risk_decisions_1h', 0)}\nallowed_to_trade: {status.get('allowed_to_trade')}"
    assert "risk_state: OK" in risk_text
    trades = [
        {"timestamp": "2026-01-01T00:00:00Z", "side": "buy", "qty": "1", "price": "100", "realized_pnl_delta": "0"},
        {"timestamp": "2026-01-01T00:01:00Z", "side": "sell", "qty": "1", "price": "101", "realized_pnl_delta": "1"},
    ]
    lines = ["Utolsó 5 trade:"] + [f"{r.get('timestamp','')} | {r.get('side','').upper()} {r.get('qty','')} @ {r.get('price','')} | pnlΔ {r.get('realized_pnl_delta','0')}" for r in trades]
    assert len(lines) == 3


def test_live_mode_fake_execution_is_forbidden(tmp_path):
    eng = make_test_engine(tmp_path, "live_fake.json", paper_mode=False, enable_live_trading=True)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 100, "size": 1}]

    with pytest.raises(RuntimeError, match="live_execution_not_implemented"):
        eng.on_candle({"high": 101, "low": 99, "close": 100})

    assert eng.state.position_size == 0.0


def test_live_execution_disabled_gate_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = BotConfig.from_env()
    cfg.paper_mode = False
    cfg.enable_live_trading = True
    cfg.live_execution_enabled = False

    with pytest.raises(RuntimeError, match="live_execution_disabled"):
        validate_live_execution_gate(cfg)


def test_live_execution_enabled_without_exchange_executor_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = BotConfig.from_env()
    cfg.paper_mode = False
    cfg.enable_live_trading = True
    cfg.live_execution_enabled = True

    with pytest.raises(RuntimeError, match="live_execution_not_implemented"):
        validate_live_execution_gate(cfg)


def test_live_execution_gate_rejects_paper_flow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = BotConfig.from_env()
    cfg.paper_mode = True
    cfg.enable_live_trading = False
    cfg.live_execution_enabled = False

    with pytest.raises(RuntimeError, match="live_only_mode_required"):
        validate_live_execution_gate(cfg)
    eng = make_test_engine(tmp_path, "paper_flow.json", paper_mode=True, enable_live_trading=False)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 100, "size": 1}]
    fills = eng.on_candle({"high": 101, "low": 99, "close": 100})
    assert len(fills) == 1


def test_live_preflight_script_does_not_print_secrets():
    secret_key = "super-secret-private-key"
    secret_token = "123456:super-secret-token"
    result = subprocess.run(
        ["bash", "scripts/live_preflight_check.sh"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "ENV_PROFILE": "paper",
            "HL_PRIVATE_KEY": secret_key,
            "TELEGRAM_BOT_TOKEN": secret_token,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    combined_output = result.stdout + result.stderr
    assert result.returncode != 0
    assert secret_key not in combined_output
    assert secret_token not in combined_output



def test_btc_live_order_normalizers_use_exchange_safe_wire_precision():
    from hyperliquid.utils.signing import float_to_wire

    price = normalize_price("BTC", 71059.67467196014)
    size = normalize_size("BTC", 0.00014072480485628953)

    assert price == 71060.0
    assert size == 0.00014
    assert float_to_wire(price) == "71060"
    assert float_to_wire(size) == "0.00014"


def test_live_submit_skips_btc_order_below_min_notional_after_normalization(tmp_path, caplog):
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def __init__(self):
            self.placed = []

        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 160.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0", "entryPx": "0"}

        def get_open_orders(self, symbol):
            return []

        def get_user_fills(self, symbol=None):
            return []

        def place_limit_order(self, symbol, side, size, price, *, reduce_only=False):
            self.placed.append((symbol, side, size, price, reduce_only))

    client = FakeLiveClient()
    eng = LiveExecutionEngine(
        client,
        str(tmp_path / "live_state.json"),
        start_balance=160,
        min_notional_usd=10,
        max_notional_per_trade_usd=50,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )

    assert not eng._submit_live_limit("BTC", "buy", 0.00014072480485628953, 71059.67467196014, reduce_only=False)
    assert client.placed == []
    assert "live_order_skipped reason=min_notional" in caplog.text



def test_live_submit_allows_reduce_only_btc_order_below_min_notional_after_normalization(tmp_path):
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def __init__(self):
            self.placed = []

        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 160.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0", "entryPx": "0"}

        def get_open_orders(self, symbol):
            return []

        def get_user_fills(self, symbol=None):
            return []

        def place_limit_order(self, symbol, side, size, price, *, reduce_only=False):
            self.placed.append((symbol, side, size, price, reduce_only))

    client = FakeLiveClient()
    eng = LiveExecutionEngine(
        client,
        str(tmp_path / "live_state.json"),
        start_balance=160,
        min_notional_usd=10,
        max_notional_per_trade_usd=50,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )

    assert eng._submit_live_limit("BTC", "sell", 0.00001, 71059.67467196014, reduce_only=True)
    assert client.placed == [("BTC", "sell", 0.00001, 71060.0, True)]


def test_live_flatten_position_uses_aggressive_reduce_only_limit_for_dust_long(tmp_path):
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def __init__(self):
            self.placed = []

        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 160.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0.00001", "entryPx": "70000"}

        def get_open_orders(self, symbol):
            return []

        def get_user_fills(self, symbol=None):
            return []

        def place_limit_order(self, symbol, side, size, price, *, reduce_only=False):
            self.placed.append((symbol, side, size, price, reduce_only))

    client = FakeLiveClient()
    eng = LiveExecutionEngine(
        client,
        str(tmp_path / "live_state.json"),
        start_balance=160,
        min_notional_usd=10,
        max_notional_per_trade_usd=50,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )

    assert eng.flatten_position("BTC", 71000.0)
    assert client.placed == [("BTC", "sell", 0.00001, 70858.0, True)]


def test_dust_position_is_held_for_next_normal_reduce_only_close(tmp_path):
    cfg = BotConfig.from_env()
    cfg.auto_dust_cleanup_usd = 5.0
    cfg.dust_position_notional_usd = 3.0
    cfg.allow_long_biased = False
    cfg.allow_short_biased = False
    eng = make_test_engine(tmp_path, "dust_cleanup.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 0.00003
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 60000, "size": 0.001}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [70000 - i for i in range(80)], "high": [70001 - i for i in range(80)], "low": [69999 - i for i in range(80)]})

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["is_dust_position"] is True
    assert status["dust_cleanup_action"] == "hold_for_next_normal_reduce_only_close"
    assert status["dust_cleanup_requested"] is True
    assert eng.state.position_size == pytest.approx(0.00003)


def test_dust_cleanup_reduce_only_once_keeps_one_order_and_respects_replace_threshold(tmp_path):
    cfg = BotConfig.from_env()
    cfg.dust_cleanup_mode = "reduce_only_once"
    cfg.dust_position_notional_usd = 2.0
    cfg.dust_cleanup_cooldown_seconds = 300
    cfg.allow_long_biased = False
    cfg.allow_short_biased = False
    eng = make_test_engine(tmp_path, "dust_reduce_once.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 0.00001
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 59000, "size": 0.001, "reduce_only": False},
        {"symbol": "BTC", "side": "sell", "price": 60000, "size": 0.001, "reduce_only": False},
    ]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [60000.0 for _ in range(80)], "high": [60001.0 for _ in range(80)], "low": [59999.0 for _ in range(80)]})

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["reason"] == "dust_cleanup_below_min_notional"
    assert status["dust_cleanup_action"] == "skipped_below_5_usd"
    assert status["dust_cleanup_in_progress"] is False
    assert len(eng.open_orders) == 2

    eng.paper.position_size = 0.0001
    cfg.dust_position_notional_usd = 10.0
    status2 = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status2["reason"] == "dust_cleanup"
    assert status2["dust_cleanup_action"] == "submitted"
    assert status2["dust_cleanup_in_progress"] is True
    assert status2["reduce_only"] is True
    assert status2["dust_cleanup_count"] == 1
    assert len(eng.open_orders) == 1
    assert eng.open_orders[0]["reduce_only"] is True
    assert eng.open_orders[0]["order_source"] == "dust_cleanup"
    assert eng.open_orders[0]["side"] == "sell"
    assert eng.open_orders[0]["size"] == pytest.approx(0.0001)

    status3 = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status3["dust_cleanup_action"] == "in_progress"
    assert len(eng.open_orders) == 1
    assert eng.open_orders[0]["price"] == pytest.approx(60000.0 * 0.998)

    eng.paper.position_size = 0.0
    status4 = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status4["dust_cleanup_in_progress"] is False
    assert status4["is_dust_position"] is False

def test_dust_cleanup_reduce_only_once_cooldown_prevents_resubmit(tmp_path):
    cfg = BotConfig.from_env()
    cfg.dust_cleanup_mode = "reduce_only_once"
    cfg.dust_position_notional_usd = 2.0
    cfg.dust_cleanup_cooldown_seconds = 300
    eng = make_test_engine(tmp_path, "dust_cooldown.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 0.00001
    orch = StrategyOrchestrator(cfg, eng)
    orch.last_dust_cleanup_ts = time.time()
    candles = pd.DataFrame({"close": [60000.0 for _ in range(80)], "high": [60001.0 for _ in range(80)], "low": [59999.0 for _ in range(80)]})

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["reason"] == "dust_cleanup_skipped_cooldown"
    assert status["dust_cleanup_action"] == "skipped_cooldown"
    assert eng.open_orders == []

def test_live_submit_sends_normalized_btc_wire_values_and_not_raw_floats(tmp_path):
    from hyperliquid.utils.signing import float_to_wire
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def __init__(self):
            self.placed = []

        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 160.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0", "entryPx": "0"}

        def get_open_orders(self, symbol):
            return []

        def get_user_fills(self, symbol=None):
            return []

        def place_limit_order(self, symbol, side, size, price, *, reduce_only=False):
            float_to_wire(price)
            float_to_wire(size)
            self.placed.append((symbol, side, size, price, reduce_only))

    client = FakeLiveClient()
    eng = LiveExecutionEngine(
        client,
        str(tmp_path / "live_state.json"),
        start_balance=160,
        min_notional_usd=10,
        max_notional_per_trade_usd=50,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )

    assert eng._submit_live_limit("BTC", "buy", 0.00028144960971257906, 71059.67467196014, reduce_only=False)
    assert client.placed == [("BTC", "buy", 0.00028, 71060.0, False)]


def test_live_submit_catches_sdk_wire_rounding_error_and_continues(tmp_path, caplog):
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 160.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0", "entryPx": "0"}

        def get_open_orders(self, symbol):
            return []

        def get_user_fills(self, symbol=None):
            return []

        def place_limit_order(self, symbol, side, size, price, *, reduce_only=False):
            raise ValueError("float_to_wire causes rounding", price)

    eng = LiveExecutionEngine(
        FakeLiveClient(),
        str(tmp_path / "live_state.json"),
        start_balance=160,
        min_notional_usd=10,
        max_notional_per_trade_usd=50,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )

    assert not eng._submit_live_limit("BTC", "buy", 0.00028144960971257906, 71059.67467196014, reduce_only=False)
    assert "live_order_skipped reason=sdk_wire_rounding_error" in caplog.text

def test_live_execution_engine_does_not_use_candle_touch_fills(tmp_path):
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def __init__(self):
            self.orders = [{"coin": "BTC", "side": "B", "limitPx": "100", "sz": "0.1", "oid": 1}]

        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 160.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0", "entryPx": "0"}

        def get_open_orders(self, symbol):
            return self.orders

        def get_user_fills(self, symbol=None):
            return []

    eng = LiveExecutionEngine(
        FakeLiveClient(),
        str(tmp_path / "live_state.json"),
        start_balance=160,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )
    fills = eng.on_candle({"symbol": "BTC", "high": 101, "low": 99, "close": 100})
    assert fills == []
    assert eng.state.position_size == 0.0
    assert not (tmp_path / "trades.csv").exists()


def test_live_execution_engine_writes_ledger_only_from_exchange_fills(tmp_path):
    from src.execution_engine import LiveExecutionEngine

    class FakeLiveClient:
        def require_live_execution_support(self):
            return None

        def get_balance(self):
            return {"equity": 161.0}

        def get_position(self, symbol):
            return {"coin": symbol, "szi": "0.001", "entryPx": "100000"}

        def get_open_orders(self, symbol):
            return []

        def get_user_fills(self, symbol=None):
            return [{"coin": "BTC", "side": "B", "px": "100000", "sz": "0.0001", "fee": "0.004", "closedPnl": "0", "time": 1760000000000, "tid": 123}]

    eng = LiveExecutionEngine(
        FakeLiveClient(),
        str(tmp_path / "live_state.json"),
        start_balance=160,
        trade_ledger_csv=str(tmp_path / "trades.csv"),
        trade_ledger_jsonl=str(tmp_path / "trades.jsonl"),
        risk_decisions_csv=str(tmp_path / "risk.csv"),
    )
    fills = eng.sync_user_fills("BTC")
    assert len(fills) == 1
    assert fills[0]["reason"] == "exchange_fill"
    assert (tmp_path / "trades.csv").read_text(encoding="utf-8").count("exchange_fill") == 1
    assert eng.consume_trade_log()[0]["symbol"] == "BTC"
    assert eng.sync_user_fills("BTC") == []


def test_live_execution_engine_missing_executor_fails(tmp_path):
    from src.execution_engine import LiveExecutionEngine

    class MissingLiveClient:
        def require_live_execution_support(self):
            raise RuntimeError("live_execution_not_implemented: nope")

    with pytest.raises(RuntimeError, match="live_execution_not_implemented"):
        LiveExecutionEngine(MissingLiveClient(), str(tmp_path / "live_state.json"), start_balance=160)


def test_live_preflight_script_does_not_echo_secret_names_as_values():
    script = Path("scripts/live_preflight_check.sh").read_text(encoding="utf-8")
    assert "HL_PRIVATE_KEY}" not in script
    assert "TELEGRAM_BOT_TOKEN}" not in script
    assert "telegram_credentials_present=true" in script


def _http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(f"HTTP {status_code}", response=response)


def test_hyperliquid_post_info_retries_retryable_http_errors(monkeypatch):
    client = HyperliquidClient("", "", network="testnet")
    sleeps = []
    attempts = {"count": 0}

    class FakeResponse:
        def raise_for_status(self):
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise _http_error(500 if attempts["count"] == 1 else 502)

        def json(self):
            return {"BTC": "100.0"}

    monkeypatch.setattr("src.hyperliquid_client.time.sleep", sleeps.append)
    monkeypatch.setattr("src.hyperliquid_client.requests.post", lambda *args, **kwargs: FakeResponse())

    assert client._post_info({"type": "allMids"}) == {"BTC": "100.0"}
    assert attempts["count"] == 3
    assert sleeps == [1, 2]


def test_hyperliquid_retry_exhaustion_raises_after_backoff(monkeypatch):
    client = HyperliquidClient("", "", network="testnet")
    sleeps = []
    attempts = {"count": 0}

    def always_timeout():
        attempts["count"] += 1
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr("src.hyperliquid_client.time.sleep", sleeps.append)

    with pytest.raises(requests.exceptions.ReadTimeout):
        client._retry_hyperliquid_api("test", always_timeout)

    assert attempts["count"] == 5
    assert sleeps == [1, 2, 5, 10]


def test_hyperliquid_retry_skips_non_retryable_http_status(monkeypatch):
    client = HyperliquidClient("", "", network="testnet")
    sleeps = []

    monkeypatch.setattr("src.hyperliquid_client.time.sleep", sleeps.append)

    with pytest.raises(requests.exceptions.HTTPError):
        client._retry_hyperliquid_api("test", lambda: (_ for _ in ()).throw(_http_error(400)))

    assert sleeps == []


def load_telegram_control_bot_module():
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "scripts" / "telegram_control_bot.py"
    spec = importlib.util.spec_from_file_location("telegram_control_bot", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_pull_restart_script_uses_fixed_command_and_timeout(monkeypatch):
    telegram_bot = load_telegram_control_bot_module()
    calls = []

    def fake_run(args, capture_output, text, timeout, check):
        calls.append(
            {
                "args": args,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(telegram_bot.subprocess, "run", fake_run)

    rc, reply = telegram_bot.run_pull_restart_script()

    assert rc == 0
    assert reply == "✅ Git pull kész, LIVE bot újraindítva."
    assert calls == [
        {
            "args": ["/usr/local/bin/hyperliquid_pull_restart.sh"],
            "capture_output": True,
            "text": True,
            "timeout": 60,
            "check": False,
        }
    ]


def test_pull_restart_script_failure_summarizes_and_redacts_secrets(monkeypatch):
    telegram_bot = load_telegram_control_bot_module()
    secret_key = "0x" + "a" * 64

    def fake_run(args, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(
            args,
            7,
            stdout=f"HL_PRIVATE_KEY={secret_key}\nstdout detail",
            stderr="TELEGRAM_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyzABCDEF\nstderr detail",
        )

    monkeypatch.setattr(telegram_bot.subprocess, "run", fake_run)

    rc, reply = telegram_bot.run_pull_restart_script()

    assert rc == 7
    assert "rc=7" in reply
    assert "stdout detail" in reply
    assert "stderr detail" in reply
    assert secret_key not in reply
    assert "123456:abcdefghijklmnopqrstuvwxyzABCDEF" not in reply
    assert "[REDACTED]" in reply


def test_pull_restart_script_timeout_returns_timeout_message(monkeypatch):
    telegram_bot = load_telegram_control_bot_module()

    def fake_run(args, capture_output, text, timeout, check):
        raise subprocess.TimeoutExpired(args, timeout, output="partial stdout", stderr="partial stderr")

    monkeypatch.setattr(telegram_bot.subprocess, "run", fake_run)

    rc, reply = telegram_bot.run_pull_restart_script()

    assert rc == 124
    assert "timeout (60s)" in reply
    assert "partial stdout" in reply
    assert "partial stderr" in reply


def _cfg_for_grid_tests(monkeypatch):
    monkeypatch.delenv("ENV_PROFILE", raising=False)
    cfg = BotConfig.from_env()
    cfg.allow_long_biased = True
    cfg.allow_short_biased = True
    cfg.grid_levels = 4
    cfg.max_notional_per_trade_usd = 10
    cfg.min_notional_usd = 1
    cfg.min_order_size = 0
    cfg.max_order_size = 10
    cfg.max_position_notional_usd = 50
    cfg.max_directional_exposure_pct = 1.0
    cfg.recenter_cooldown_seconds = 3600
    cfg.stale_order_max_age_sec = 1800
    return cfg


def test_flat_one_sell_order_valid_when_only_sells_allowed(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "orphan_sell.json", paper_mode=True)
    eng.open_orders = [{"symbol": "BTC", "side": "sell", "price": 201.0, "size": 0.01}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [200 - i for i in range(80)], "high": [201 - i for i in range(80)], "low": [199 - i for i in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["allow_buys"] is False
    assert status["allow_sells"] is True
    assert status["expected_buy_orders"] == 0
    assert status["expected_sell_orders"] == 1
    assert status["actual_buy_orders"] == 0
    assert status["actual_sell_orders"] == 1
    assert status["orphan_decision_reason"] == "active_allowed_sides_present"
    assert status["orphan_order_detected"] is False
    assert status["orphan_order_cleanup_count"] == 0
    assert status["cleanup_canceled_orders"] == 0


def test_flat_one_buy_order_valid_when_only_buys_allowed(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "orphan_buy.json", paper_mode=True)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 0.1}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["allow_buys"] is True
    assert status["allow_sells"] is False
    assert status["expected_buy_orders"] == 1
    assert status["expected_sell_orders"] == 0
    assert status["actual_buy_orders"] == 1
    assert status["actual_sell_orders"] == 0
    assert status["orphan_decision_reason"] == "active_allowed_sides_present"
    assert status["orphan_order_detected"] is False
    assert status["orphan_order_cleanup_count"] == 0
    assert status["cleanup_canceled_orders"] == 0


def test_flat_missing_allowed_sell_side_triggers_orphan_cleanup(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "orphan_missing_sell.json", paper_mode=True)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 199.0, "size": 0.01}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [200 - i for i in range(80)], "high": [201 - i for i in range(80)], "low": [199 - i for i in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["allow_buys"] is False
    assert status["allow_sells"] is True
    assert status["expected_buy_orders"] == 0
    assert status["expected_sell_orders"] == 1
    assert status["actual_buy_orders"] == 1
    assert status["actual_sell_orders"] == 0
    assert status["orphan_decision_reason"] == "expected_active_side_missing:sell"
    assert status["orphan_order_detected"] is True
    assert status["orphan_order_cleanup_count"] == 1
    assert status["cleanup_canceled_orders"] == 1


def test_flat_missing_allowed_buy_side_triggers_orphan_cleanup(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "orphan_missing_buy.json", paper_mode=True)
    eng.open_orders = [{"symbol": "BTC", "side": "sell", "price": 101.0, "size": 0.1}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 + i for i in range(80)], "high": [101 + i for i in range(80)], "low": [99 + i for i in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["allow_buys"] is True
    assert status["allow_sells"] is False
    assert status["expected_buy_orders"] == 1
    assert status["expected_sell_orders"] == 0
    assert status["actual_buy_orders"] == 0
    assert status["actual_sell_orders"] == 1
    assert status["orphan_decision_reason"] == "expected_active_side_missing:buy"
    assert status["orphan_order_detected"] is True
    assert status["orphan_order_cleanup_count"] == 1
    assert status["cleanup_canceled_orders"] == 1


def test_flat_no_orders_rebuilds_grid_even_without_price_recenter(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "flat_no_orders.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    first = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    assert first["status"] == "running"
    eng.open_orders = []

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["flat_without_orders"] is True
    assert status["force_recenter"] is True
    assert len(eng.open_orders) > 0


def test_long_position_sell_tp_is_not_orphan_deleted(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "long_tp.json", paper_mode=True)
    eng.paper.position_size = 1.0
    eng.open_orders = [{"symbol": "BTC", "side": "sell", "price": 102.0, "size": 1.0, "reduce_only": True}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=40.0)

    assert status["orphan_order_detected"] is False
    assert status["cleanup_canceled_orders"] == 0


def test_short_position_buy_tp_is_not_orphan_deleted(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "short_tp.json", paper_mode=True)
    eng.paper.position_size = -1.0
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 98.0, "size": 1.0, "reduce_only": True}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=40.0)

    assert status["orphan_order_detected"] is False
    assert status["cleanup_canceled_orders"] == 0


def test_stale_flat_order_older_than_timeout_cancelled(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    eng = make_test_engine(tmp_path, "stale.json", paper_mode=True)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 0.1, "created_ts": time.time() - 3600}]
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["stale_order_detected"] is True
    assert status["stale_order_cleanup_count"] == 1
    assert status["cleanup_canceled_orders"] == 1


def test_daily_pnl_calculation_uses_daily_start_equity_baseline():
    from src.main import calculate_daily_pnl_metrics

    metrics = calculate_daily_pnl_metrics(
        current_equity=161.74,
        daily_start_equity=161.475,
        realized_pnl_total=1.265,
        daily_start_realized_pnl=1.0,
        unrealized_pnl=0.0,
        fees_paid_total=0.12,
        daily_start_fees_paid=0.10,
    )

    assert metrics["daily_pnl"] == pytest.approx(0.265)
    assert metrics["daily_pnl_pct"] == pytest.approx(0.265 / 161.475)
    assert metrics["daily_realized_pnl"] == pytest.approx(0.265)
    assert metrics["daily_fees"] == pytest.approx(0.02)


def test_small_equity_grid_does_not_exceed_max_active_exposure(tmp_path, monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    cfg.grid_levels = 8
    cfg.max_notional_per_trade_usd = 12
    cfg.min_notional_usd = 8
    cfg.max_position_notional_usd = 40
    cfg.max_directional_exposure_pct = 1.0
    eng = make_test_engine(tmp_path, "small_equity.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})

    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    buy_exposure = sum(o["price"] * o["size"] for o in eng.open_orders if o["side"] == "buy")
    sell_exposure = sum(o["price"] * o["size"] for o in eng.open_orders if o["side"] == "sell")
    assert status["status"] == "running"
    assert buy_exposure <= 40.0 + 1e-6
    assert sell_exposure <= 40.0 + 1e-6


def test_small_equity_safe_missing_env_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ENV_PROFILE", raising=False)
    monkeypatch.delenv("GRID_LEVELS", raising=False)
    monkeypatch.delenv("MAX_POSITION_NOTIONAL_USD", raising=False)
    monkeypatch.delenv("STALE_ORDER_MAX_AGE_SEC", raising=False)
    monkeypatch.delenv("ORDER_NOTIONAL_USD", raising=False)
    monkeypatch.delenv("MAX_NOTIONAL_PER_TRADE_USD", raising=False)
    monkeypatch.delenv("MAX_ACTIVE_EXPOSURE_USD", raising=False)
    cfg = BotConfig.from_env()
    assert cfg.grid_levels == 3
    assert cfg.max_position_notional_usd == 50
    assert cfg.stale_order_max_age_sec == 1800
    assert 8 <= cfg.order_notional_usd <= 15
    assert 30 <= cfg.max_active_exposure_usd <= 50


def test_max_active_exposure_caps_grid_notional(tmp_path):
    cfg = BotConfig.from_env()
    cfg.grid_levels = 4
    cfg.max_position_notional_usd = 500
    cfg.max_active_exposure_usd = 50
    cfg.max_directional_exposure_pct = 1.0
    cfg.order_notional_usd = 15
    cfg.max_notional_per_trade_usd = 15
    cfg.min_notional_usd = 1
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "exposure_cap.json", paper_mode=True, enable_live_trading=False))
    levels = orch._exposure_limited_grid_levels(GridMode.NEUTRAL, True, True, 15, cfg.max_active_exposure_usd)
    assert levels * 15 <= 50


def test_trend_bias_defaults_and_confidence(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "trend_bias.json", paper_mode=True, enable_live_trading=False))
    assert orch._trend_bias(0.5) == pytest.approx(0.70)
    assert orch._trend_bias(0.9) == pytest.approx(0.80)
    assert orch._trend_bias(0.1) == pytest.approx(0.60)


def test_grid_level_counts_use_70_30_default_bias():
    gm = GridManager()
    buy, sell = gm._level_counts(10, GridMode.LONG_BIASED, trend_bias=0.70)
    assert (buy, sell) == (7, 3)
    buy, sell = gm._level_counts(10, GridMode.SHORT_BIASED, trend_bias=0.70)
    assert (buy, sell) == (3, 7)


def test_volatility_adjusted_grid_levels(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "vol_levels.json", paper_mode=True, enable_live_trading=False))
    assert orch._volatility_adjusted_grid_levels(cfg.vol_low_threshold / 2) == 4
    assert orch._volatility_adjusted_grid_levels((cfg.vol_low_threshold + cfg.vol_high_threshold) / 2) == 3
    assert orch._volatility_adjusted_grid_levels(cfg.vol_high_threshold * 1.1) == 2
    assert orch._volatility_adjusted_grid_levels(cfg.vol_extreme_threshold * 1.1) == 1


def test_final_effective_grid_levels_flat_floor_prevents_collapse(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "flat_floor.json", paper_mode=True, enable_live_trading=False))

    levels, reason = orch._final_effective_grid_levels(
        configured_levels=4,
        volatility_grid_levels=1,
        risk_adjusted_levels=1,
        position_side="FLAT",
        allow_buys=True,
        allow_sells=False,
        order_notional=10.0,
        threshold=5.0,
    )

    assert levels == 2
    assert "flat_min_grid_levels_floor:2" in reason
    assert "risk_floor_override_while_flat" in reason


def test_grid_diagnostic_report_counts_distribution():
    from src import main

    report = {}
    status = {
        "final_effective_levels": 2,
        "force_recenter": True,
        "rebuild_reason": "flat_without_orders",
        "orphan_order_detected": True,
        "grid_level_reduction_reason": "flat_min_grid_levels_floor:2",
        "no_grid_orders_generated_reason": "edge_filter_removed_all_orders",
    }

    updated = main._update_grid_diagnostic_report(report, status, no_grid_orders_generated=True)

    assert updated["cycles"] == 1
    assert updated["rebuild_count"] == 1
    assert updated["no_grid_orders_generated_count"] == 1
    assert updated["orphan_cleanup_count"] == 1
    assert updated["effective_grid_levels_distribution"] == {"2": 1}


def test_atr_spacing_increases_with_volatility(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "atr_spacing.json", paper_mode=True, enable_live_trading=False))
    low_spacing, low_source = orch._calculate_spacing_pct(0.001, 0.001)
    high_spacing, high_source = orch._calculate_spacing_pct(0.02, 0.001)
    assert high_spacing > low_spacing
    assert "atr" in high_source


def test_orphan_cleanup_forced_rebuild_bypasses_cooldown(tmp_path):
    cfg = BotConfig.from_env()
    cfg.recenter_cooldown_seconds = 3600
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_notional_usd = 1
    eng = make_test_engine(tmp_path, "orphan_cleanup_forced.json", paper_mode=True, enable_live_trading=False)
    old_ts = time.time() - 10000
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99, "size": 0.1, "created_ts": old_ts}]
    orch = StrategyOrchestrator(cfg, eng)
    orch.last_recenter_ts = datetime.now(timezone.utc)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    assert status["force_recenter"] is True
    assert status["forced_rebuild"] is True
    assert status["rebuild_reason"] in {"orphan_cleanup", "stale_cleanup"}


def test_stale_cleanup_forced_rebuild_bypasses_cooldown(tmp_path):
    cfg = BotConfig.from_env()
    cfg.recenter_cooldown_seconds = 3600
    cfg.stale_order_max_age_sec = 1
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_notional_usd = 1
    eng = make_test_engine(tmp_path, "stale_cleanup_forced.json", paper_mode=True, enable_live_trading=False)
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 99, "size": 0.1, "created_ts": time.time() - 10},
        {"symbol": "BTC", "side": "sell", "price": 101, "size": 0.1, "created_ts": time.time() - 10},
    ]
    orch = StrategyOrchestrator(cfg, eng)
    orch.last_recenter_ts = datetime.now(timezone.utc)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    assert status["force_recenter"] is True
    assert status["forced_rebuild"] is True
    assert status["rebuild_reason"] == "stale_cleanup"


class FakeLiveStateEngine:
    def __init__(self):
        self.paper_mode = False
        self.open_orders = []
        self.no_fill_cycles = 0
        self.last_real_trade_ts = None
        self.paper = type("PaperMirror", (), {"position_size": 9.0, "avg_entry": 1.0})()

    def account_state(self, symbol, mark_price):
        from src.execution_engine import AccountState
        return AccountState("live", 160.0, 0.01, 100.0, 1.0, 0.0, 0.0, 0.0, 160.0, 0.0, 0.0, "2026-06-05")

    def cancel_replace_grid(self, symbol, plan):
        self.open_orders = [{"symbol": symbol, "side": o.side, "price": o.price, "size": o.size} for o in plan.long_levels + plan.short_levels]
        return {"canceled": 0, "placed": len(self.open_orders), "symbol": symbol}

    def cancel_all_orders(self, symbol):
        self.open_orders = []
        return 0

    def flatten_position(self, symbol, price):
        return False

    def place_reduce_only_orders(self, symbol, position_size, mark_price, order_size, levels=3):
        return 0


def test_live_strategy_uses_live_account_state_not_paper_mirror():
    cfg = BotConfig.from_env()
    cfg.paper_mode = False
    cfg.max_position_notional_usd = 50
    cfg.max_active_exposure_usd = 50
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_notional_usd = 1
    orch = StrategyOrchestrator(cfg, FakeLiveStateEngine())
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [101 for _ in range(80)], "low": [99 for _ in range(80)]})
    status = orch.on_tick(candles, equity=9999.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=9999.0)
    assert status["state_source"] == "live"
    assert status["position_notional_raw"] == pytest.approx(9999.0)  # explicit caller override is still honored
    status = orch.on_tick(candles, equity=9999.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    assert status["position_notional_raw"] == pytest.approx(1.0)
    assert status["is_dust_position"] is True
    assert status["effective_position_side"] == "FLAT"


def test_daily_pnl_baseline_calculation_is_equity_based():
    from src.main import calculate_daily_pnl_metrics
    metrics = calculate_daily_pnl_metrics(
        current_equity=161.0,
        daily_start_equity=160.0,
        realized_pnl_total=2.0,
        daily_start_realized_pnl=1.0,
        unrealized_pnl=0.5,
        fees_paid_total=0.2,
        daily_start_fees_paid=0.1,
    )
    assert metrics["daily_realized_pnl"] == pytest.approx(1.0)
    assert metrics["daily_fees"] == pytest.approx(0.1)
    assert metrics["net_daily_pnl"] == pytest.approx(1.0)
    assert metrics["daily_pnl_pct"] == pytest.approx(1.0 / 160.0)
    assert metrics["daily_pnl_pct"] < 0.61


def test_minimum_expected_edge_filter_fails_open_for_flat_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIN_EXPECTED_NET_EDGE_USD", "999")
    cfg = BotConfig.from_env()
    eng = make_test_engine(tmp_path, "state.json")
    orchestrator = StrategyOrchestrator(cfg, eng)
    plan = GridManager().build_grid(100, 2, 0.003, 0.0, MarketRegime.RANGE, 1, 0.0, 0.0, GridMode.NEUTRAL)

    edge = orchestrator._apply_minimum_expected_edge_filter(plan, mid_price=100, position_size=0.0)

    assert edge["edge_filter_skipped_orders"] == 0
    assert len(plan.long_levels) == 2
    assert len(plan.short_levels) == 2
    assert edge["expected_net_edge"] is not None


def test_trade_analytics_report_calculates_profitability_metrics(tmp_path):
    from src.main import _trade_analytics_report

    trades = tmp_path / "trades.csv"
    trades.write_text(
        "timestamp,symbol,side,price,qty,notional,fee,realized_pnl_delta,realized_pnl_total,cash,equity,position_before,position_after,position_size,position_notional,exposure_action,trade_category,is_flip_trade,flip_trade_count,regime,mode,risk_state,pause_reason,reason\n"
        "2026-01-01T00:00:00+00:00,BTC,buy,10000,1,10000,0.04,0,0,500,500,0,1,1,100,increasing,entry,False,0,RANGE,neutral,OK,,grid_fill\n"
        "2026-01-01T00:10:00+00:00,BTC,sell,10100,1,10100,0.04,1,1,501,501,1,0,0,0,flat,close,False,0,RANGE,neutral,OK,,grid_fill\n"
        "2026-01-01T00:20:00+00:00,BTC,sell,10000,1,10000,0.04,0,1,501,501,0,-1,-1,100,increasing,entry,False,0,TREND_DOWN,short_biased,OK,,grid_fill\n"
        "2026-01-01T00:30:00+00:00,BTC,buy,10100,1,10100,0.04,-1,0,500,500,-1,0,0,0,flipping,flip,True,1,TREND_DOWN,short_biased,OK,,grid_fill\n"
    )

    report = _trade_analytics_report("BTC", trades)

    assert report["avg_winner"] == pytest.approx(1.0)
    assert report["avg_loser"] == pytest.approx(-1.0)
    assert report["profit_factor"] == pytest.approx(1.0)
    assert report["expectancy"] == pytest.approx(-0.04)
    assert report["fee_gross_profit_ratio"] == pytest.approx(0.16)
    assert report["average_holding_time_seconds"] == pytest.approx(600.0)
    assert report["pnl_by_regime"]["RANGE"] == pytest.approx(0.92)
    assert report["pnl_by_regime"]["TREND_DOWN"] == pytest.approx(-1.08)
    assert report["pnl_by_trade_category"]["flip"] == pytest.approx(-1.04)
    assert report["flip_trade_count"] == 1
    assert report["flip_trade_pnl"] == pytest.approx(-1.04)


class DummyLiveClient:
    def require_live_execution_support(self):
        return None

    def get_balance(self):
        return {"equity": 1234.0}

    def get_position(self, symbol):
        return {"szi": "0.02", "entryPx": "100.0", "unrealizedPnl": "1.5"}

    def get_open_orders(self, symbol):
        return []

    def get_user_fills(self, symbol=None):
        return []

    def cancel_all_orders(self, symbol):
        return 0

    def place_limit_order(self, symbol, side, size, price, reduce_only=False):
        return {"status": "ok"}


def test_live_execution_engine_is_not_paper_engine(tmp_path):
    engine = LiveExecutionEngine(DummyLiveClient(), str(tmp_path / "live_state.json"), max_notional_per_trade_usd=10, min_notional_usd=1)

    assert not isinstance(engine, PaperExecutionEngine)
    assert not hasattr(engine, "paper")
    assert engine.execution_mode == "live_only"
    with pytest.raises(RuntimeError, match="live_execution_forbids_paper_simulation"):
        engine._require_paper_execution("on_candle")


def test_live_execution_account_state_uses_unified_execution_state(tmp_path):
    engine = LiveExecutionEngine(DummyLiveClient(), str(tmp_path / "live_state.json"), max_notional_per_trade_usd=10, min_notional_usd=1)

    state = engine.account_state("BTC", 101.0)

    assert state.state_source == "live"
    assert state.equity == pytest.approx(1234.0)
    assert state.position_size == pytest.approx(0.02)
    assert engine.state.position_size == pytest.approx(0.02)

from src.market_stress import OpportunityMode, VolatilityMode, decide_market_stress


def _stress_cfg():
    cfg = BotConfig.from_env()
    cfg.enable_market_stress_filter = True
    cfg.high_vol_atr_pct = 0.006
    cfg.extreme_vol_atr_pct = 0.010
    cfg.btc_5m_shock_pct = 0.008
    cfg.btc_1h_shock_pct = 0.025
    cfg.trend_strong_confidence = 0.75
    cfg.trend_weak_confidence = 0.45
    cfg.panic_score_threshold = 0.80
    cfg.market_stress_extreme_flat = False
    cfg.grid_levels = 3
    cfg.min_notional_usd = 1.0
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.max_active_exposure_usd = 50
    cfg.max_position_notional_usd = 50
    cfg.max_directional_exposure_pct = 1.0
    return cfg


def _stress_candles(last_move: float = 0.009, periods: int = 80):
    closes = [100.0 for _ in range(periods - 1)] + [100.0 * (1 + last_move)]
    return pd.DataFrame({"close": closes, "high": [c * 1.006 for c in closes], "low": [c * 0.994 for c in closes]})


def test_market_stress_high_vol_strong_trend_down_from_flat_allows_sells_blocks_buys():
    decision = decide_market_stress(
        candles=_stress_candles(-0.009), raw_regime=MarketRegime.TREND_DOWN, confirmed_regime=MarketRegime.TREND_DOWN,
        regime_confidence=0.80, atr_pct=0.007, position_side="FLAT", position_notional=0.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.TREND_FOLLOW_SHORT
    assert decision.allow_sells is True
    assert decision.allow_buys is False
    assert decision.grid_levels == 2


def test_market_stress_high_vol_strong_trend_up_from_flat_allows_buys_blocks_sells():
    decision = decide_market_stress(
        candles=_stress_candles(0.009), raw_regime=MarketRegime.TREND_UP, confirmed_regime=MarketRegime.TREND_UP,
        regime_confidence=0.80, atr_pct=0.007, position_side="FLAT", position_notional=0.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.TREND_FOLLOW_LONG
    assert decision.allow_buys is True
    assert decision.allow_sells is False
    assert decision.grid_levels == 2


def test_market_stress_high_vol_weak_range_from_flat_blocks_new_exposure():
    decision = decide_market_stress(
        candles=_stress_candles(0.009), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.007, position_side="FLAT", position_notional=0.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.NO_NEW_EXPOSURE
    assert decision.allow_buys is False
    assert decision.allow_sells is False


def test_market_stress_high_vol_existing_short_allows_buy_reduce():
    decision = decide_market_stress(
        candles=_stress_candles(0.009), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.007, position_side="SHORT", position_notional=25.0, config=_stress_cfg(),
        allow_buys=False, allow_sells=True, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.NO_NEW_EXPOSURE
    assert decision.allow_buys is True
    assert decision.allow_sells is False


def test_market_stress_high_vol_existing_long_allows_sell_reduce():
    decision = decide_market_stress(
        candles=_stress_candles(-0.009), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.007, position_side="LONG", position_notional=25.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=False, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.NO_NEW_EXPOSURE
    assert decision.allow_buys is False
    assert decision.allow_sells is True


def test_market_stress_extreme_vol_weak_trend_reduce_only():
    decision = decide_market_stress(
        candles=_stress_candles(0.02), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.011, position_side="LONG", position_notional=25.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert decision.volatility_mode == VolatilityMode.EXTREME
    assert decision.opportunity_mode == OpportunityMode.REDUCE_ONLY
    assert decision.reduce_only is True
    assert decision.allow_sells is True
    assert decision.allow_buys is False


def test_market_stress_emergency_flat_only_when_enabled():
    cfg = _stress_cfg()
    cfg.market_stress_extreme_flat = False
    disabled = decide_market_stress(
        candles=_stress_candles(0.02), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.011, position_side="LONG", position_notional=25.0, config=cfg,
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    cfg.market_stress_extreme_flat = True
    enabled = decide_market_stress(
        candles=_stress_candles(0.02), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.011, position_side="LONG", position_notional=25.0, config=cfg,
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert disabled.opportunity_mode == OpportunityMode.REDUCE_ONLY
    assert disabled.emergency_flat is False
    assert enabled.opportunity_mode == OpportunityMode.EMERGENCY_FLAT
    assert enabled.emergency_flat is True


def test_market_stress_normal_range_unchanged_behavior():
    decision = decide_market_stress(
        candles=_stress_candles(0.001), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.30, atr_pct=0.003, position_side="FLAT", position_notional=0.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.NORMAL_GRID
    assert decision.allow_buys is True
    assert decision.allow_sells is True
    assert decision.grid_levels == 3
    assert decision.spacing_multiplier == 1.0


def test_market_stress_fields_appear_in_status_payload(tmp_path):
    cfg = _stress_cfg()
    engine = make_test_engine(tmp_path, "market_stress_status.json", paper_mode=True, enable_live_trading=False)
    orch = StrategyOrchestrator(cfg, engine)
    candles = pd.DataFrame({"close": [100 for _ in range(80)], "high": [100.2 for _ in range(80)], "low": [99.8 for _ in range(80)]})
    status = orch.on_tick(candles, equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)
    for field in ["market_stress_enabled", "volatility_mode", "market_stress_score", "trend_strength_score", "opportunity_mode", "btc_5m_return_pct", "btc_1h_return_pct", "market_stress_reason"]:
        assert field in status


def test_market_stress_missing_or_short_candle_data_does_not_crash():
    decision = decide_market_stress(
        candles=pd.DataFrame({"close": [100.0]}), raw_regime=MarketRegime.RANGE, confirmed_regime=MarketRegime.RANGE,
        regime_confidence=0.0, atr_pct=0.0, position_side="FLAT", position_notional=0.0, config=_stress_cfg(),
        allow_buys=True, allow_sells=True, current_levels=3,
    )
    assert decision.opportunity_mode == OpportunityMode.NORMAL_GRID
    assert decision.allow_buys is True
    assert decision.allow_sells is True


def test_spacing_multiplier_and_floor_reduce_grid_density(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_SPACING_MULTIPLIER", "1.20")
    monkeypatch.setenv("GRID_SPACING_MIN_PCT", "0.0018")
    cfg = BotConfig.from_env()
    cfg.grid_spacing_pct = 0.003
    cfg.grid_spacing_vol_multiplier = 1.2
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "spacing_multiplier.json", paper_mode=True, enable_live_trading=False))

    spacing, source = orch._calculate_spacing_pct(atr_pct=0.002, return_vol_pct=0.001)

    assert spacing == pytest.approx(0.0030)
    assert source == "low_volatility_atr14_x1.20_configured_cap"
    assert cfg.grid_spacing_min_pct == pytest.approx(0.0018)


def test_trade_analytics_tracks_fee_regime_dust_and_direction(tmp_path):
    from src.main import _trade_analytics_report

    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "timestamp,symbol,side,price,qty,notional,fee,fill_liquidity,dust_fill,realized_pnl_delta,position_size,trade_category,regime\n"
        "2026-06-01T00:00:00+00:00,BTC,buy,10000,0.01,100,0.010,maker,false,0.050,1,entry,RANGE\n"
        "2026-06-01T01:00:00+00:00,BTC,sell,10100,0.01,101,0.030,taker,false,-0.020,0,close,TREND_DOWN\n"
        "2026-06-01T02:00:00+00:00,BTC,buy,10000,0.00001,0.1,0.001,maker,true,0.010,0,entry,RANGE\n",
        encoding="utf-8",
    )

    report = _trade_analytics_report("BTC", csv_path, exclude_dust=True)

    assert report["dust_trade_count"] == 1
    assert report["maker_trades"] == 1
    assert report["taker_trades"] == 1
    assert report["maker_fee_total"] == pytest.approx(0.010)
    assert report["taker_fee_total"] == pytest.approx(0.030)
    assert report["trades_by_regime"] == {"RANGE": 1, "TREND_DOWN": 1}
    assert report["pnl_long_trades"] == pytest.approx(0.040)
    assert report["pnl_short_trades"] == pytest.approx(-0.050)


def test_regime_hysteresis_turns_fast_uptrend_reversal_into_pullback():
    detector = RegimeDetector(trend_hold_ticks=4)
    candles = pd.DataFrame({"close": [100.0 + i for i in range(40)], "high": [101.0 + i for i in range(40)], "low": [99.0 + i for i in range(40)]})

    up_signal = RegimeSignal(
        MarketRegime.TREND_UP, 0.90, 0.004, 0.004, 0.02, 0.006, 0.005,
        trend_structure_score=0.8,
        momentum_score=0.8,
        trend_strength_score=0.85,
        higher_high_sequence_score=1.0,
        higher_low_sequence_score=1.0,
    )
    pullback_raw_signal = RegimeSignal(
        MarketRegime.TREND_DOWN, 0.62, 0.004, 0.004, -0.01, -0.002, 0.001,
        trend_structure_score=0.2,
        momentum_score=-0.4,
        trend_strength_score=0.55,
        higher_high_sequence_score=0.6,
        higher_low_sequence_score=0.4,
    )

    detector._detect_raw_signal = lambda *args, **kwargs: up_signal
    assert detector.detect_signal(candles, ema_slope_threshold_pct=0.004).regime == MarketRegime.TREND_UP

    detector._detect_raw_signal = lambda *args, **kwargs: pullback_raw_signal
    signal = detector.detect_signal(candles, ema_slope_threshold_pct=0.004)

    assert signal.regime == MarketRegime.TREND_UP_PULLBACK
    assert signal.regime_hysteresis_score > 0.0
    assert signal.confidence <= 0.45


def test_regime_hysteresis_allows_confirmed_uptrend_to_downtrend_flip():
    detector = RegimeDetector(trend_hold_ticks=4)
    candles = pd.DataFrame({"close": [100.0 + i for i in range(40)], "high": [101.0 + i for i in range(40)], "low": [99.0 + i for i in range(40)]})

    up_signal = RegimeSignal(
        MarketRegime.TREND_UP, 0.90, 0.004, 0.004, 0.02, 0.006, 0.005,
        trend_structure_score=0.8,
        momentum_score=0.8,
        trend_strength_score=0.85,
        higher_high_sequence_score=1.0,
        higher_low_sequence_score=1.0,
    )
    reversal_signal = RegimeSignal(
        MarketRegime.TREND_DOWN, 0.88, 0.004, 0.004, -0.03, -0.008, -0.006,
        trend_structure_score=-1.0,
        momentum_score=-0.9,
        trend_strength_score=0.92,
        higher_high_sequence_score=-1.0,
        higher_low_sequence_score=-1.0,
        trend_transition_score=-0.9,
        transition_direction="DOWN",
        transition_confidence=0.85,
    )

    detector._detect_raw_signal = lambda *args, **kwargs: up_signal
    assert detector.detect_signal(candles, ema_slope_threshold_pct=0.004).regime == MarketRegime.TREND_UP

    detector._detect_raw_signal = lambda *args, **kwargs: reversal_signal
    signal = detector.detect_signal(candles, ema_slope_threshold_pct=0.004)

    assert signal.regime == MarketRegime.TREND_DOWN
    assert signal.regime_hysteresis_score <= -0.35


def test_pullback_regime_keeps_trend_biased_grid_mode(tmp_path):
    cfg = BotConfig.from_env()
    cfg.regime_confirmation_bars = 1
    cfg.regime_min_confidence = 0.0
    cfg.allow_long_biased = True
    eng = make_test_engine(tmp_path, "pullback_mode.json", paper_mode=True, enable_live_trading=False)
    orch = StrategyOrchestrator(cfg, eng)
    candles = pd.DataFrame({"close": [100.0 for _ in range(80)], "high": [101.0 for _ in range(80)], "low": [99.0 for _ in range(80)]})
    pullback_signal = RegimeSignal(
        MarketRegime.TREND_UP_PULLBACK, 0.45, 0.004, 0.004, -0.01, -0.002, 0.001,
        trend_structure_score=0.2,
        trend_strength_score=0.55,
        higher_high_sequence_score=0.6,
        higher_low_sequence_score=0.4,
        regime_hysteresis_score=0.3,
    )
    orch.detector.detect_signal = lambda *args, **kwargs: pullback_signal

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC")

    assert status["regime"] == "TREND_UP_PULLBACK"
    assert status["mode"] == GridMode.LONG_BIASED.value
    assert status["regime_hysteresis_score"] == 0.3


def test_counter_trend_entry_filter_blocks_weak_edge_shorts_in_bullish_momentum(tmp_path):
    cfg = BotConfig.from_env()
    cfg.counter_trend_entry_edge_score = 10.0
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "counter_trend_filter.json", paper_mode=True, enable_live_trading=False))
    signal = RegimeSignal(
        MarketRegime.TREND_UP,
        0.9,
        0.002,
        0.001,
        0.01,
        0.01,
        0.01,
        momentum_score=0.8,
        ema_slope_acceleration_score=0.7,
        transition_direction="UP",
        transition_confidence=0.8,
    )

    decision = orch._apply_counter_trend_entry_filter(
        allow_buys=True,
        allow_sells=True,
        position_side="FLAT",
        regime=MarketRegime.TREND_UP,
        regime_signal=signal,
        spacing_pct=0.003,
        fee_rate=0.0004,
        force_reduce_only=False,
    )

    assert decision["allow_buys"] is True
    assert decision["allow_sells"] is False
    assert decision["short_entry_blocked_by_trend_filter"] is True


def test_counter_trend_entry_filter_blocks_weak_edge_longs_in_bearish_momentum(tmp_path):
    cfg = BotConfig.from_env()
    cfg.counter_trend_entry_edge_score = 10.0
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "counter_trend_long_filter.json", paper_mode=True, enable_live_trading=False))
    signal = RegimeSignal(
        MarketRegime.TREND_DOWN,
        0.9,
        0.002,
        0.001,
        -0.01,
        -0.01,
        -0.01,
        momentum_score=-0.8,
        ema_slope_acceleration_score=-0.7,
        transition_direction="DOWN",
        transition_confidence=0.8,
    )

    decision = orch._apply_counter_trend_entry_filter(
        allow_buys=True,
        allow_sells=True,
        position_side="FLAT",
        regime=MarketRegime.RANGE,
        regime_signal=signal,
        spacing_pct=0.003,
        fee_rate=0.0004,
        force_reduce_only=False,
    )

    assert decision["allow_buys"] is False
    assert decision["allow_sells"] is True
    assert decision["long_entry_blocked_by_trend_filter"] is True


def test_minimum_expected_edge_filter_does_not_apply_net_profit_fee_multiple_to_openings(tmp_path):
    cfg = BotConfig.from_env()
    cfg.min_expected_net_profit_fee_multiple = 2.5
    cfg.min_net_profit_fee_multiplier = 2.5
    eng = make_test_engine(tmp_path, "net_fee_multiple.json")
    orch = StrategyOrchestrator(cfg, eng)
    plan = GridManager().build_grid(100, 1, 0.0025, 0.0, MarketRegime.RANGE, 1, 0.0, 0.0, GridMode.NEUTRAL)

    edge = orch._apply_minimum_expected_edge_filter(plan, mid_price=100, position_size=0.0)

    assert edge["edge_filter_skipped_by_reason"] == {}
    assert edge["expected_profit_after_fees"] is not None
    assert len(plan.long_levels) == 1
    assert len(plan.short_levels) == 1


def test_adverse_move_protection_closes_before_reversion_confirmation(tmp_path):
    cfg = BotConfig.from_env()
    cfg.adverse_move_exit_pct = 0.0045
    cfg.min_reversion_confirmation_pct = 0.0015
    cfg.max_position_notional_usd = 500
    eng = make_test_engine(tmp_path, "adverse_move.json", paper_mode=True, enable_live_trading=False)
    eng.paper.position_size = 1.0
    eng.paper.avg_entry = 100.0
    orch = StrategyOrchestrator(cfg, eng)
    orch.wrong_way_exit_loss_pct = 0.99
    candles = pd.DataFrame({"close": [100.0 for _ in range(78)] + [99.6, 99.4], "high": [101.0 for _ in range(80)], "low": [99.0 for _ in range(80)]})

    status = orch.on_tick(candles, equity=1000.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=99.4)

    assert status["reason"] == "adverse_move_protection_exit_requested"
    assert status["flattened"] is True
    assert status["adverse_move_pct"] > cfg.adverse_move_exit_pct


def test_volatility_adaptive_spacing_uses_configured_buckets(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "vol_bucket_spacing.json", paper_mode=True, enable_live_trading=False))

    low_spacing, low_source = orch._calculate_spacing_pct(0.001, 0.001)
    normal_spacing, normal_source = orch._calculate_spacing_pct(0.006, 0.001)
    high_spacing, high_source = orch._calculate_spacing_pct(0.02, 0.001)

    assert low_spacing >= 0.0035
    assert 0.0035 <= normal_spacing <= 0.0045
    assert 0.0055 <= high_spacing <= 0.0055
    assert "low_volatility" in low_source
    assert "normal_volatility" in normal_source
    assert "high_volatility" in high_source


def test_trade_analytics_report_groups_by_side_regime_hour_and_volatility_bucket(tmp_path):
    from src.main import _trade_analytics_report

    trades = tmp_path / "grouped_trades.csv"
    trades.write_text(
        "timestamp,symbol,side,price,qty,notional,fee,realized_pnl_delta,position_size,regime,trade_category,volatility_bucket\n"
        "2026-01-01T05:00:00+00:00,BTC,buy,10000,1,10000,0.04,1,1,RANGE,entry,low\n"
        "2026-01-01T05:10:00+00:00,BTC,sell,10100,1,10100,0.04,-0.5,0,TREND_UP,close,high\n"
        "2026-01-01T13:00:00+00:00,BTC,sell,10000,1,10000,0.04,2,-1,TREND_DOWN,entry,normal\n"
    )

    report = _trade_analytics_report("BTC", trades)

    assert report["performance_by_side"]["buy"]["trades"] == 1
    assert report["performance_by_regime"]["TREND_UP"]["pnl"] == pytest.approx(-0.54)
    assert report["performance_by_hour"]["05:00"]["trades"] == 2
    assert report["performance_by_volatility_bucket"]["high"]["pnl"] == pytest.approx(-0.54)


def _flat_pipeline_cfg(monkeypatch):
    cfg = _cfg_for_grid_tests(monkeypatch)
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_order_notional_usd = 10
    cfg.min_notional_usd = 10
    cfg.max_active_exposure_usd = 50
    cfg.max_position_notional_usd = 50
    cfg.max_directional_exposure_pct = 1.0
    cfg.grid_levels = 2
    cfg.min_grid_levels = 1
    cfg.enable_market_stress_filter = False
    cfg.trend_flip_cooldown_ticks = 0
    cfg.min_expected_net_edge_usd = 0
    cfg.min_expected_net_edge_pct = 0
    cfg.min_rr_ratio = 1.0
    cfg.min_net_profit_fee_multiplier = 999.0
    cfg.min_expected_net_profit_fee_multiple = 999.0
    cfg.anti_chop_cooldown_minutes = 5
    return cfg


def _force_signal(orch, regime):
    orch.detector.detect_signal = lambda *args, **kwargs: RegimeSignal(
        regime=regime,
        confidence=0.9,
        atr_pct=0.003,
        return_vol_pct=0.001,
        move_pct=0.0,
        ema_slope_fast_pct=0.0,
        ema_slope_slow_pct=0.0,
        trend_strength_score=0.9 if regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN} else 0.0,
    )


def _flat_candles(price=100.0):
    return pd.DataFrame({"close": [price for _ in range(80)], "high": [price + 1 for _ in range(80)], "low": [price - 1 for _ in range(80)]})


def test_flat_range_neutral_submits_buy_and_sell_orders(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    eng = make_test_engine(tmp_path, "flat_range_orders.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["status"] == "running"
    assert status["mode"] == "neutral"
    assert status["orders"]["placed"] >= 2
    assert {o["side"] for o in eng.open_orders} == {"buy", "sell"}
    assert status["skip_reason"] == "none"


def test_flat_trend_down_short_biased_submits_sell_orders_if_allowed(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    eng = make_test_engine(tmp_path, "flat_down_orders.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.TREND_DOWN)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["mode"] == "short_biased"
    assert status["allow_sells"] is True
    assert status["allow_buys"] is False
    assert len(eng.open_orders) > 0
    assert all(o["side"] == "sell" for o in eng.open_orders)


def test_flat_trend_up_long_biased_submits_buy_orders_if_allowed(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    eng = make_test_engine(tmp_path, "flat_up_orders.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.TREND_UP)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["mode"] == "long_biased"
    assert status["allow_buys"] is True
    assert status["allow_sells"] is False
    assert len(eng.open_orders) > 0
    assert all(o["side"] == "buy" for o in eng.open_orders)


def test_min_notional_blocks_only_below_threshold_orders(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    cfg.order_notional_usd = 9
    cfg.max_notional_per_trade_usd = 9
    cfg.min_order_notional_usd = 10
    eng = make_test_engine(tmp_path, "min_notional_block.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["raw_grid_orders"] > 0
    assert status["after_min_notional"] == 0
    assert status["final_orders_to_submit"] == 0
    assert status["skip_reason"] == "no_allowed_sides"
    assert eng.open_orders == []


def test_net_profit_filter_does_not_block_opening_orders(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    cfg.min_net_profit_fee_multiplier = 10_000.0
    cfg.min_expected_net_profit_fee_multiple = 10_000.0
    eng = make_test_engine(tmp_path, "net_profit_fail_open.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["edge_filter_skipped_orders"] == 0
    assert status["orders"]["placed"] >= 2
    assert {o["side"] for o in eng.open_orders} == {"buy", "sell"}


def test_dust_filter_does_not_block_normal_flat_entries(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    cfg.dust_position_notional_usd = 2
    eng = make_test_engine(tmp_path, "dust_flat_entries.json", paper_mode=True)
    eng.paper.position_size = 0.01
    eng.paper.avg_entry = 100.0
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["is_dust_position"] is True
    assert status["effective_position_side"] == "FLAT"
    assert status["orders"]["placed"] >= 2
    assert {o["side"] for o in eng.open_orders} == {"buy", "sell"}


def test_anti_chop_logs_and_expires_correctly(tmp_path, monkeypatch, caplog):
    cfg = _flat_pipeline_cfg(monkeypatch)
    eng = make_test_engine(tmp_path, "anti_chop.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)
    orch.anti_chop_cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=1)

    with caplog.at_level(logging.WARNING):
        active_status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert active_status["anti_chop_cooldown_active"] is True
    assert active_status["final_orders_to_submit"] >= 2
    assert active_status["orders"]["placed"] >= 2
    assert "anti_chop_flat_fail_open_override" in caplog.text
    assert "anti_chop_active" in caplog.text

    caplog.clear()
    orch.anti_chop_cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired_status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert expired_status["anti_chop_cooldown_active"] is False
    assert expired_status["final_orders_to_submit"] >= 2
    assert {o["side"] for o in eng.open_orders} == {"buy", "sell"}


def _l2_snapshot(bid_sizes, ask_sizes):
    return {
        "levels": [
            [{"px": str(100 - i), "sz": str(sz)} for i, sz in enumerate(bid_sizes)],
            [{"px": str(101 + i), "sz": str(sz)} for i, sz in enumerate(ask_sizes)],
        ]
    }


def test_orderbook_analyzer_smooths_and_classifies_strong_bullish():
    from src.orderbook_analyzer import OrderBookAnalyzer

    analyzer = OrderBookAnalyzer(top_levels=10, smoothing_window=3, min_samples=3)
    snapshot = _l2_snapshot([2.0] * 10, [1.0] * 10)

    first = analyzer.analyze(snapshot)
    second = analyzer.analyze(snapshot)
    third = analyzer.analyze(snapshot)

    assert first.ready is False
    assert second.ready is False
    assert third.ready is True
    assert third.bid_volume_top10 == pytest.approx(20.0)
    assert third.ask_volume_top10 == pytest.approx(10.0)
    assert third.imbalance_ratio == pytest.approx(2.0)
    assert third.pressure_score == pytest.approx(1.0 / 3.0)
    assert third.classification == "Strong Bullish"
    assert third.long_size_multiplier == pytest.approx(1.25)
    assert third.short_size_multiplier == pytest.approx(0.75)


def test_strategy_orderbook_unavailable_fails_open(tmp_path):
    cfg = BotConfig.from_env()
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_order_notional_usd = 1
    cfg.min_notional_usd = 1
    cfg.regrid_threshold_pct = 0.0
    eng = make_test_engine(tmp_path, "orderbook_fail_open.json", paper_mode=True, enable_live_trading=False)
    orch = StrategyOrchestrator(cfg, eng)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0)

    assert status["orderbook_available"] is False
    assert status["decision"] == "orderbook_unavailable"
    assert status["allowed_to_trade"] is True
    assert status["final_orders_to_submit"] > 0


def test_strategy_orderbook_bearish_filters_flat_longs_in_downtrend(tmp_path, monkeypatch):
    cfg = BotConfig.from_env()
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_order_notional_usd = 1
    cfg.min_notional_usd = 1
    cfg.regrid_threshold_pct = 0.0
    cfg.orderbook_min_samples = 3
    cfg.orderbook_soft_mode = False
    cfg.orderbook_counter_edge_score = 999.0
    cfg.regime_confirmation_bars = 1
    cfg.regime_min_confidence = 0.1
    eng = make_test_engine(tmp_path, "orderbook_filter.json", paper_mode=True, enable_live_trading=False)
    orch = StrategyOrchestrator(cfg, eng)
    bearish = _l2_snapshot([1.0] * 10, [2.0] * 10)

    signal = RegimeSignal(
        regime=MarketRegime.RANGE,
        confidence=0.95,
        atr_pct=0.003,
        return_vol_pct=0.001,
        move_pct=-0.01,
        ema_slope_fast_pct=-0.01,
        ema_slope_slow_pct=-0.01,
        trend_structure_score=1.0,
        momentum_score=1.0,
        trend_strength_score=1.0,
    )
    monkeypatch.setattr(orch.detector, "detect_signal", lambda *args, **kwargs: signal)

    for _ in range(2):
        orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0, order_book_snapshot=bearish)
    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0, order_book_snapshot=bearish)

    assert status["ready"] is True
    assert status["classification"] == "Strong Bearish"
    assert status["allow_buys"] is False
    assert status["allow_sells"] is True
    assert status["orderbook_decision"] == "counter_orderbook_edge_too_low"
    assert status["orderbook_filtered_entries"] >= 1


def test_orderbook_stale_fails_open_without_blocking_trading(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    cfg.orderbook_max_age_seconds = 5
    eng = make_test_engine(tmp_path, "orderbook_stale_fail_open.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)
    stale_snapshot = {**_l2_snapshot([10.0] * 10, [1.0] * 10), "received_ts": time.time() - 10}

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0, order_book_snapshot=stale_snapshot)

    assert status["orderbook_available"] is False
    assert status["stale"] is True
    assert status["orderbook_action_taken"] == "fallback_existing_strategy"
    assert status["allowed_to_trade"] is True
    assert status["final_orders_to_submit"] > 0


def test_orderbook_malformed_fails_open_without_blocking_trading(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    eng = make_test_engine(tmp_path, "orderbook_malformed_fail_open.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0, order_book_snapshot={"levels": "bad"})

    assert status["orderbook_available"] is False
    assert status["orderbook_action_taken"] == "fallback_existing_strategy"
    assert status["allowed_to_trade"] is True
    assert status["final_orders_to_submit"] > 0


def test_orderbook_soft_mode_never_zero_orders_while_flat_risk_ok(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    cfg.grid_levels = 2
    cfg.orderbook_min_samples = 1
    cfg.orderbook_soft_mode = True
    cfg.orderbook_counter_edge_score = 999.0
    eng = make_test_engine(tmp_path, "orderbook_soft_no_zero.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)
    bearish = _l2_snapshot([1.0] * 10, [10.0] * 10)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0, order_book_snapshot=bearish)

    assert status["orderbook_soft_mode"] is True
    assert status["allow_buys"] is True
    assert status["allow_sells"] is True
    assert status["orderbook_action_taken"] == "reduce_grid_levels"
    assert status["final_orders_to_submit"] > 0
    assert len(eng.open_orders) > 0


def test_orderbook_level_reduction_capped_by_config_pct(tmp_path, monkeypatch):
    cfg = _flat_pipeline_cfg(monkeypatch)
    cfg.grid_levels = 4
    cfg.min_grid_levels = 4
    cfg.max_grid_levels = 4
    cfg.orderbook_min_samples = 1
    cfg.orderbook_soft_mode = True
    cfg.orderbook_counter_edge_score = 999.0
    cfg.orderbook_max_level_reduction_pct = 0.25
    eng = make_test_engine(tmp_path, "orderbook_reduction_cap.json", paper_mode=True)
    orch = StrategyOrchestrator(cfg, eng)
    _force_signal(orch, MarketRegime.RANGE)
    bullish = _l2_snapshot([10.0] * 10, [1.0] * 10)

    status = orch.on_tick(_flat_candles(), equity=160.0, daily_pnl_pct=0.0, symbol="BTC", position_notional=0.0, order_book_snapshot=bullish)

    assert status["orderbook_levels_before"] == 4
    assert status["orderbook_levels_after"] >= 3
    assert status["orderbook_grid_level_multiplier"] == pytest.approx(0.75)


def test_orderbook_hard_mode_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ORDERBOOK_SOFT_MODE", raising=False)
    monkeypatch.delenv("ORDERBOOK_FILTER_ENABLED", raising=False)
    cfg = BotConfig.from_env()

    assert cfg.orderbook_filter_enabled is True
    assert cfg.orderbook_soft_mode is True

from src.prediction_layer import PredictionLayer, PredictionResult


def _prediction_result(bias="NEUTRAL", confidence=0.0, score=0.0, available=True):
    return PredictionResult(
        available=available,
        directional_score=score,
        bullish_probability=0.5 if score > 0 else 0.2,
        bearish_probability=0.5 if score < 0 else 0.2,
        neutral_probability=0.3,
        confidence_score=confidence,
        prediction_bias=bias,
        contributing_signals={"momentum_score": score},
        fallback_reason="" if available else "test_unavailable",
    )


def test_prediction_layer_weighted_scoring_outputs_probabilities():
    layer = PredictionLayer()
    candles = pd.DataFrame({
        "close": [100 + i * 0.2 for i in range(60)],
        "high": [101 + i * 0.2 for i in range(60)],
        "low": [99 + i * 0.2 for i in range(60)],
        "volume": [100 + i for i in range(60)],
    })
    signal = RegimeSignal(
        MarketRegime.TREND_UP,
        0.8,
        0.004,
        0.002,
        0.01,
        0.004,
        0.003,
        momentum_score=0.8,
        ema_slope_acceleration_score=0.7,
    )

    prediction = layer.predict(
        candles=candles,
        regime=MarketRegime.TREND_UP,
        regime_confidence=0.8,
        regime_signal=signal,
        orderbook={"available": True, "stale": False, "pressure_score": 0.8},
        ema_slope_threshold_pct=0.003,
    )

    assert prediction.available is True
    assert prediction.directional_score > 0
    assert prediction.bullish_probability > prediction.bearish_probability
    assert prediction.prediction_bias in {"LONG", "STRONG_LONG"}
    assert pytest.approx(prediction.bullish_probability + prediction.bearish_probability + prediction.neutral_probability) == 1.0


def test_prediction_unavailable_strategy_unchanged(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_unavailable.json", paper_mode=True, enable_live_trading=False))
    unavailable = _prediction_result(available=False)

    entry = orch._apply_prediction_entry_filter(
        allow_buys=True,
        allow_sells=True,
        position_side="FLAT",
        regime=MarketRegime.TREND_UP,
        prediction=unavailable,
        force_reduce_only=False,
    )
    plan = GridManager().build_grid(100, 4, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)
    before = (len(plan.long_levels), len(plan.short_levels), [lvl.size for lvl in plan.long_levels], [lvl.size for lvl in plan.short_levels])
    grid = orch._apply_prediction_grid_bias(plan, price=100, prediction=unavailable, position_side="FLAT", max_one_side_notional=1000)
    after = (len(plan.long_levels), len(plan.short_levels), [lvl.size for lvl in plan.long_levels], [lvl.size for lvl in plan.short_levels])

    assert entry["allow_buys"] is True
    assert entry["allow_sells"] is True
    assert entry["prediction_entry_action"] == "fallback_existing_strategy"
    assert before == after
    assert grid["prediction_grid_action"] == "fallback_existing_strategy"


def test_range_long_prediction_keeps_both_sides_and_biases_long(tmp_path):
    cfg = BotConfig.from_env()
    cfg.prediction_max_level_bias_pct = 0.5
    cfg.prediction_max_size_multiplier = 1.25
    cfg.prediction_min_size_multiplier = 0.75
    cfg.max_order_size = 10
    cfg.max_notional_per_trade_usd = 1000
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_range_long.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 4, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    context = orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("LONG", 0.8, 0.4), position_side="FLAT", max_one_side_notional=1000)

    assert len(plan.long_levels) > 0
    assert len(plan.short_levels) > 0
    assert len(plan.long_levels) > 4
    assert len(plan.short_levels) < 4
    assert context["long_level_multiplier"] > 1.0
    assert context["short_level_multiplier"] < 1.0
    assert plan.long_levels[0].size > 1.0
    assert plan.short_levels[0].size < 1.0
    assert context["prediction_grid_action"] == "range_long_bias"


def test_range_short_prediction_keeps_both_sides_and_biases_short(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_order_size = 10
    cfg.max_notional_per_trade_usd = 1000
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_range_short.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 4, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    context = orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("SHORT", 0.8, -0.4), position_side="FLAT", max_one_side_notional=1000)

    assert len(plan.long_levels) > 0
    assert len(plan.short_levels) > 0
    assert len(plan.short_levels) > 4
    assert len(plan.long_levels) < 4
    assert context["short_level_multiplier"] > 1.0
    assert context["long_level_multiplier"] < 1.0
    assert plan.short_levels[0].size > 1.0
    assert plan.long_levels[0].size < 1.0
    assert context["prediction_grid_action"] == "range_short_bias"


def test_trend_up_does_not_allow_shorts_unless_strong_short_prediction(tmp_path):
    cfg = BotConfig.from_env()
    cfg.prediction_soft_mode = False
    cfg.prediction_confidence_threshold = 0.65
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_trend_up.json", paper_mode=True, enable_live_trading=False))

    weak = orch._apply_prediction_entry_filter(allow_buys=True, allow_sells=True, position_side="FLAT", regime=MarketRegime.TREND_UP, prediction=_prediction_result("SHORT", 0.65, -0.4), force_reduce_only=False)
    strong = orch._apply_prediction_entry_filter(allow_buys=True, allow_sells=False, position_side="FLAT", regime=MarketRegime.TREND_UP, prediction=_prediction_result("STRONG_SHORT", 0.9, -0.8), force_reduce_only=False)

    assert weak["allow_sells"] is False
    assert strong["allow_sells"] is True


def test_trend_down_does_not_allow_longs_unless_strong_long_prediction(tmp_path):
    cfg = BotConfig.from_env()
    cfg.prediction_soft_mode = False
    cfg.prediction_confidence_threshold = 0.65
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_trend_down.json", paper_mode=True, enable_live_trading=False))

    weak = orch._apply_prediction_entry_filter(allow_buys=True, allow_sells=True, position_side="FLAT", regime=MarketRegime.TREND_DOWN, prediction=_prediction_result("LONG", 0.65, 0.4), force_reduce_only=False)
    strong = orch._apply_prediction_entry_filter(allow_buys=False, allow_sells=True, position_side="FLAT", regime=MarketRegime.TREND_DOWN, prediction=_prediction_result("STRONG_LONG", 0.9, 0.8), force_reduce_only=False)

    assert weak["allow_buys"] is False
    assert strong["allow_buys"] is True


def test_prediction_layer_cannot_produce_zero_order_flat_state(tmp_path):
    cfg = BotConfig.from_env()
    cfg.prediction_soft_mode = True
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_no_zero.json", paper_mode=True, enable_live_trading=False))
    entry = orch._apply_prediction_entry_filter(
        allow_buys=False,
        allow_sells=False,
        position_side="FLAT",
        regime=MarketRegime.TREND_UP,
        prediction=_prediction_result("LONG", 0.9, 0.8),
        force_reduce_only=False,
    )
    plan = GridManager().build_grid(100, 1, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)
    orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("STRONG_LONG", 1.0, 0.9), position_side="FLAT", max_one_side_notional=1000)

    assert entry["allow_buys"] is False
    assert entry["allow_sells"] is False
    assert len(plan.long_levels) >= 1
    assert len(plan.short_levels) >= 1



def test_strong_long_never_removes_sell_side_while_flat(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_order_size = 10
    cfg.max_notional_per_trade_usd = 1000
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_strong_long_keep_sell.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 1, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    context = orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("STRONG_LONG", 0.9, 0.8), position_side="FLAT", max_one_side_notional=1000)

    assert len(plan.long_levels) >= 1
    assert len(plan.short_levels) >= 1
    assert context["long_size_multiplier"] <= cfg.prediction_max_size_multiplier


def test_strong_short_never_removes_buy_side_while_flat(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_order_size = 10
    cfg.max_notional_per_trade_usd = 1000
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_strong_short_keep_buy.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 1, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    context = orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("STRONG_SHORT", 0.9, -0.8), position_side="FLAT", max_one_side_notional=1000)

    assert len(plan.long_levels) >= 1
    assert len(plan.short_levels) >= 1
    assert context["short_size_multiplier"] <= cfg.prediction_max_size_multiplier


def test_prediction_size_multipliers_respect_trade_caps(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_order_size = 10
    cfg.max_notional_per_trade_usd = 105
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_size_caps.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 2, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("STRONG_LONG", 0.9, 0.8), position_side="FLAT", max_one_side_notional=1000)

    assert all(level.size * level.price <= cfg.max_notional_per_trade_usd + 1e-9 for level in plan.long_levels + plan.short_levels)


def test_prediction_telemetry_fields_are_present(tmp_path):
    cfg = BotConfig.from_env()
    cfg.max_order_size = 10
    cfg.max_notional_per_trade_usd = 1000
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "pred_telemetry.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 2, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    context = orch._apply_prediction_grid_bias(plan, price=100, prediction=_prediction_result("LONG", 0.9, 0.8), position_side="FLAT", max_one_side_notional=1000)

    for field in ["long_level_multiplier", "short_level_multiplier", "long_size_multiplier", "short_size_multiplier"]:
        assert field in context

def test_analyze_closed_trades_reconstructs_fifo_fees_and_hold_times(tmp_path):
    import importlib.util
    import sys

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "analyze_closed_trades.py"
    spec = importlib.util.spec_from_file_location("analyze_closed_trades", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    rows = [
        {"timestamp": "2026-01-01T00:00:00+00:00", "symbol": "BTC", "side": "buy", "qty": "1", "fee": "0.10", "realized_pnl_delta": "0", "regime": "RANGE"},
        {"timestamp": "2026-01-01T01:00:00+00:00", "symbol": "BTC", "side": "sell", "qty": "0.5", "fee": "0.05", "realized_pnl_delta": "5", "regime": "RANGE"},
        {"timestamp": "2026-01-01T02:00:00+00:00", "symbol": "BTC", "side": "sell", "qty": "0.5", "fee": "0.05", "realized_pnl_delta": "-2", "regime": "TREND_DOWN"},
    ]

    trades = module.reconstruct_closed_trades(rows)

    assert len(trades) == 2
    assert trades[0].gross_pnl == 5
    assert trades[0].entry_fee == pytest.approx(0.05)
    assert trades[0].exit_fee == pytest.approx(0.05)
    assert trades[0].net_pnl == pytest.approx(4.90)
    assert trades[0].hold_seconds == 3600
    assert trades[1].net_pnl == pytest.approx(-2.10)
    assert trades[1].hold_seconds == 7200


def test_analyze_closed_trades_report_answers_required_questions():
    import importlib.util
    import sys

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "analyze_closed_trades.py"
    spec = importlib.util.spec_from_file_location("analyze_closed_trades", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    trades = [
        module.ClosedTrade(None, "BTC", "long", 1, 1.0, 0.1, 0.1, 60, "RANGE", "NEUTRAL", "OK", "grid_fill", "maker", ""),
        module.ClosedTrade(None, "BTC", "long", 1, -3.0, 0.1, 0.1, 120, "TREND_DOWN", "NEUTRAL", "OK", "grid_fill", "maker", ""),
    ]

    report = module.build_report(trades, 300)

    assert "Average winner" in report
    assert "Profit factor after fees" in report
    assert "1. What causes net negative expectancy?" in report
    assert "2. Are winners too small?" in report
    assert "3. Are losers too large?" in report
    assert "4. What parameter change would improve expectancy most?" in report


def test_trade_expectancy_report_reconstructs_windows_and_groups(tmp_path):
    from scripts.trade_expectancy_report import read_ledger, reconstruct_closed_trades, build_report, summarize

    ledger = tmp_path / "trades.jsonl"
    rows = [
        {
            "timestamp": "2026-01-01T05:00:00+00:00",
            "symbol": "BTC",
            "side": "buy",
            "price": 100.0,
            "qty": 1.0,
            "notional": 100.0,
            "fee": 0.10,
            "regime": "RANGE",
            "orderbook_classification": "Bullish",
            "prediction_bias": "LONG",
        },
        {
            "timestamp": "2026-01-01T05:30:00+00:00",
            "symbol": "BTC",
            "side": "sell",
            "price": 101.0,
            "qty": 1.0,
            "notional": 101.0,
            "fee": 0.10,
            "realized_pnl_delta": 1.0,
            "regime": "RANGE",
            "orderbook_classification": "Bullish",
            "prediction_bias": "LONG",
        },
        {
            "timestamp": "2026-01-01T06:00:00+00:00",
            "symbol": "BTC",
            "side": "sell",
            "price": 100.0,
            "qty": 1.0,
            "notional": 9.0,
            "fee": 0.10,
            "regime": "TREND_DOWN",
            "orderbook_classification": "Bearish",
            "prediction_bias": "SHORT",
        },
        {
            "timestamp": "2026-01-01T06:45:00+00:00",
            "symbol": "BTC",
            "side": "buy",
            "price": 101.0,
            "qty": 1.0,
            "notional": 1.5,
            "fee": 0.10,
            "realized_pnl_delta": -1.0,
            "regime": "TREND_DOWN",
            "orderbook_classification": "Bearish",
            "prediction_bias": "SHORT",
        },
    ]
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    loaded_rows = read_ledger(ledger)
    trades = reconstruct_closed_trades(loaded_rows)
    metrics = summarize(trades, len(loaded_rows))
    report = build_report(loaded_rows, trades, [2], True)

    assert metrics["closed_trade_count"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["average_winner"] == pytest.approx(0.8)
    assert metrics["average_loser"] == pytest.approx(-1.2)
    assert metrics["long_only_pnl"] == pytest.approx(0.8)
    assert metrics["short_only_pnl"] == pytest.approx(-1.2)
    assert metrics["dust_trade_count_under_2_usd"] == 1
    assert metrics["below_min_notional_fill_count_under_10_usd"] == 1
    assert "## Window: last 2" in report
    assert "## Telegram/performance report section" in report
    assert "## Recommendation" in report
    assert "### By side" in report
    assert "### By hour of day (UTC)" in report
    assert "### By regime" in report
    assert "### By orderbook classification" in report
    assert "### By prediction bias" in report


def test_trend_down_long_exception_requires_prediction_and_bullish_orderbook(tmp_path):
    cfg = BotConfig.from_env()
    cfg.prediction_confidence_threshold = 0.65
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "long_selectivity.json", paper_mode=True, enable_live_trading=False))

    blocked = orch._apply_long_side_selectivity(
        allow_buys=True,
        allow_sells=True,
        position_side="FLAT",
        regime=MarketRegime.TREND_DOWN,
        prediction=_prediction_result("LONG", 0.9, 0.8),
        orderbook={"classification": "Bearish"},
        force_reduce_only=False,
    )
    allowed = orch._apply_long_side_selectivity(
        allow_buys=True,
        allow_sells=True,
        position_side="FLAT",
        regime=MarketRegime.TREND_DOWN,
        prediction=_prediction_result("LONG", 0.71, 0.8),
        orderbook={"classification": "Strong Bullish"},
        force_reduce_only=False,
    )

    assert blocked["allow_buys"] is False
    assert blocked["long_selectivity_blocked"] is True
    assert allowed["allow_buys"] is True
    assert allowed["long_selectivity_action"] == "allow_trend_down_long_exception"


def test_range_neutral_orderbook_reduces_long_levels_without_disabling(tmp_path):
    cfg = BotConfig.from_env()
    orch = StrategyOrchestrator(cfg, make_test_engine(tmp_path, "range_long_reduce.json", paper_mode=True, enable_live_trading=False))
    plan = GridManager().build_grid(100, 4, 0.01, 0.0, MarketRegime.RANGE, 1.0, 0.0, 0.0, GridMode.NEUTRAL)

    context = orch._apply_range_orderbook_long_level_reduction(
        plan,
        position_side="FLAT",
        orderbook={"available": True, "ready": True, "stale": False, "classification": "Neutral"},
    )

    assert context["range_orderbook_long_action"] == "reduce_long_levels_25pct"
    assert len(plan.long_levels) == 3
    assert len(plan.short_levels) == 4
    assert len(plan.long_levels) > 0


def test_expectancy_telemetry_reports_side_pf_and_fee_leakage_sources(tmp_path):
    from src.main import _trade_analytics_report

    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "timestamp,symbol,side,price,qty,notional,fee,dust_fill,realized_pnl_delta,trade_category,order_source,orderbook_classification\n"
        "2026-01-01T00:00:00+00:00,BTC,buy,10000,0.001,10,0.1,false,1,close,normal_entry,Bullish\n"
        "2026-01-01T01:00:00+00:00,BTC,buy,10000,0.0003,3,0.1,false,-1,close,dust_cleanup,Bearish\n"
        "2026-01-01T02:00:00+00:00,BTC,sell,10000,0.001,10,0.1,false,2,close,normal_entry,Strong Bearish\n",
        encoding="utf-8",
    )

    metrics = _trade_analytics_report("BTC", csv_path=csv_path, exclude_dust=False)

    assert metrics["long_trade_count"] == 2
    assert metrics["short_trade_count"] == 1
    assert metrics["long_profit_factor"] == pytest.approx(0.9 / 1.1)
    assert metrics["short_profit_factor"] == pytest.approx(1.9)
    assert metrics["fills_under_10_usd"] == 1
    assert metrics["fills_under_5_usd"] == 1
    assert metrics["dust_cleanup_count"] == 1
    assert metrics["sub_10_fill_sources"] == {"dust_cleanup": 1}
    assert metrics["bullish_entry_pnl"] == pytest.approx(0.9)
    assert metrics["bearish_entry_pnl"] == pytest.approx(0.8)


def _candles_for_watchdog(price: float = 100.0, rows: int = 80) -> pd.DataFrame:
    return pd.DataFrame({"open": [price] * rows, "high": [price * 1.001] * rows, "low": [price * 0.999] * rows, "close": [price] * rows})


def test_no_fill_watchdog_narrows_spacing_after_timeout(tmp_path):
    cfg = BotConfig.from_env()
    cfg.no_fill_watchdog_enabled = True
    cfg.no_fill_max_minutes = 60
    cfg.no_fill_max_nearest_distance_pct = 0.004
    cfg.tick_seconds = 60
    cfg.min_order_notional_usd = 1
    cfg.min_notional_usd = 1
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_order_size = 0
    cfg.grid_spacing_pct = 0.01
    cfg.grid_spacing_pct_min = 0.001
    cfg.grid_spacing_low_vol_min_pct = 0.006
    cfg.grid_spacing_low_vol_max_pct = 0.012
    cfg.min_grid_profit_over_fees_pct = 0.0005
    cfg.regrid_threshold_pct = 0
    cfg.min_order_lifetime_seconds = 0
    cfg.stale_order_max_age_sec = 999999
    eng = make_test_engine(tmp_path, "watchdog.json", paper_mode=True, min_order_lifetime_seconds=0)
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 99.0, "size": 0.1, "created_ts": time.time() - 3600},
        {"symbol": "BTC", "side": "sell", "price": 101.0, "size": 0.1, "created_ts": time.time() - 3600},
    ]
    eng.no_fill_cycles = 61
    orch = StrategyOrchestrator(cfg, eng)

    status = orch.on_tick(_candles_for_watchdog(100.0), equity=500, daily_pnl_pct=0, symbol="BTC")

    assert status["no_fill_watchdog_active"] is True
    assert status["grid_spacing_pct"] < 0.012
    assert len(eng.open_orders) > 0
    assert all(float(o["price"]) * float(o["size"]) >= cfg.min_order_notional_usd for o in eng.open_orders)


def test_reprice_does_not_happen_before_minimum_lifetime(tmp_path):
    eng = make_test_engine(tmp_path, "reprice_age.json", min_order_lifetime_seconds=90, min_reprice_distance_pct=0.0015)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 1.0, "created_ts": time.time()}]
    plan = GridManager().build_grid(100, 1, 0.005, 0, MarketRegime.RANGE, 1.0, 0, 0, GridMode.NEUTRAL, allow_sells=False, force_recenter=True)

    result = eng.cancel_replace_grid("BTC", plan)

    assert result["reprice_decision"] == "skip"
    assert result["reprice_reason"] == "min_order_lifetime"
    assert eng.open_orders[0]["price"] == 99.0


def test_reprice_only_happens_if_price_delta_exceeds_threshold(tmp_path):
    eng = make_test_engine(tmp_path, "reprice_delta.json", min_order_lifetime_seconds=0, min_reprice_distance_pct=0.0015)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 1.0, "created_ts": time.time() - 120}]
    plan = GridManager().build_grid(100, 1, 0.0105, 0, MarketRegime.RANGE, 1.0, 0, 0, GridMode.NEUTRAL, allow_sells=False, force_recenter=True)
    result = eng.cancel_replace_grid("BTC", plan)
    assert result["reprice_decision"] == "skip"
    assert result["reprice_reason"] == "min_reprice_distance"

    plan = GridManager().build_grid(100, 1, 0.013, 0, MarketRegime.RANGE, 1.0, 0, 0, GridMode.NEUTRAL, allow_sells=False, force_recenter=True)
    result = eng.cancel_replace_grid("BTC", plan)
    assert result["reprice_decision"] == "replace"
    assert result["canceled"] == 1
    assert eng.open_orders[0]["price"] == pytest.approx(98.7)


def test_emergency_risk_reduce_orders_bypass_lifetime_rule(tmp_path):
    eng = make_test_engine(tmp_path, "reduce_bypass.json", min_order_lifetime_seconds=9999)
    eng.paper.position_size = 1.0
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 1.0, "created_ts": time.time()}]

    placed = eng.place_reduce_only_orders("BTC", position_size=1.0, mark_price=100.0, order_size=0.5, levels=1)

    assert placed == 1
    assert any(o.get("reduce_only") and o["side"] == "sell" for o in eng.open_orders)


def test_no_fill_watchdog_never_creates_below_min_notional_order(tmp_path):
    cfg = BotConfig.from_env()
    cfg.no_fill_watchdog_enabled = True
    cfg.no_fill_max_minutes = 60
    cfg.no_fill_max_nearest_distance_pct = 0.004
    cfg.tick_seconds = 60
    cfg.min_order_notional_usd = 50
    cfg.min_notional_usd = 50
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_order_size = 0
    cfg.grid_spacing_pct = 0.01
    cfg.grid_spacing_pct_min = 0.001
    cfg.grid_spacing_low_vol_min_pct = 0.006
    cfg.grid_spacing_low_vol_max_pct = 0.012
    cfg.min_grid_profit_over_fees_pct = 0.0005
    cfg.regrid_threshold_pct = 0
    cfg.min_order_lifetime_seconds = 0
    cfg.stale_order_max_age_sec = 999999
    eng = make_test_engine(tmp_path, "watchdog_min.json", paper_mode=True, min_order_lifetime_seconds=0, min_order_notional_usd=50)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 99.0, "size": 1.0, "created_ts": time.time() - 3600}]
    eng.no_fill_cycles = 61
    orch = StrategyOrchestrator(cfg, eng)

    orch.on_tick(_candles_for_watchdog(100.0), equity=500, daily_pnl_pct=0, symbol="BTC")

    assert all(float(o["price"]) * float(o["size"]) >= cfg.min_order_notional_usd for o in eng.open_orders)


def test_no_fill_watchdog_never_creates_zero_order_flat_state(tmp_path):
    cfg = BotConfig.from_env()
    cfg.no_fill_watchdog_enabled = True
    cfg.no_fill_max_minutes = 60
    cfg.no_fill_max_nearest_distance_pct = 0.004
    cfg.tick_seconds = 60
    cfg.min_order_notional_usd = 1
    cfg.min_notional_usd = 1
    cfg.order_notional_usd = 10
    cfg.max_notional_per_trade_usd = 10
    cfg.min_order_size = 0
    cfg.grid_spacing_pct = 0.01
    cfg.grid_spacing_pct_min = 0.001
    cfg.grid_spacing_low_vol_min_pct = 0.006
    cfg.grid_spacing_low_vol_max_pct = 0.012
    cfg.min_grid_profit_over_fees_pct = 0.0005
    cfg.regrid_threshold_pct = 0
    cfg.min_order_lifetime_seconds = 0
    cfg.stale_order_max_age_sec = 999999
    eng = make_test_engine(tmp_path, "watchdog_zero.json", paper_mode=True, min_order_lifetime_seconds=0, min_order_notional_usd=1)
    eng.open_orders = [
        {"symbol": "BTC", "side": "buy", "price": 99.0, "size": 0.1, "created_ts": time.time() - 3600},
        {"symbol": "BTC", "side": "sell", "price": 101.0, "size": 0.1, "created_ts": time.time() - 3600},
    ]
    eng.no_fill_cycles = 61
    orch = StrategyOrchestrator(cfg, eng)

    status = orch.on_tick(_candles_for_watchdog(100.0), equity=500, daily_pnl_pct=0, symbol="BTC")

    assert status["no_fill_watchdog_active"] is True
    assert len(eng.open_orders) > 0
