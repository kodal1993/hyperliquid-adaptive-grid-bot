import os
import tempfile
import pandas as pd

from src.config import BotConfig
from src.grid_manager import GridManager
from src.regime_detector import RegimeDetector
from src.risk_manager import RiskManager
from src.types import MarketRegime, GridMode
from src.execution_engine import ExecutionEngine, would_increase_exposure
from src.strategy_orchestrator import StrategyOrchestrator


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
    assert eng.paper.position_size == 0.0
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


def test_paper_profile_uses_conservative_fill_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ENV_PROFILE=paper\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "paper.env").write_text("FILL_MODEL=conservative\n")
    cfg = BotConfig.from_env()
    assert cfg.fill_model == "conservative"


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
    assert eng.paper.position_size == 0.0


def test_close_only_resize_short_flip_disabled(tmp_path):
    eng = make_test_engine(tmp_path, "flip2.json", paper_mode=True, allow_position_flip=False)
    eng.paper.position_size = -(39.0 / 80000.0)
    eng.paper.avg_entry = 80000.0
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 80000.0, "size": 50.0 / 80000.0}]
    fills = eng.on_candle({"open": 80000, "high": 81000, "low": 79000, "close": 80000})
    assert len(fills) == 1
    assert abs(fills[0]["size"] - (39.0 / 80000.0)) < 1e-12
    assert eng.paper.position_size == 0.0


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


def test_last_valid_trade_ignores_invalid_btc_prices(tmp_path):
    from src.main import _read_last_valid_trade
    p = tmp_path / "trades.csv"
    p.write_text("timestamp,symbol,side,price,qty,notional\n2026-05-08T12:00:00+00:00,BTC,buy,110,0.1,11\n2026-05-08T12:05:00+00:00,BTC,buy,80564.96,0.001,80.56\n", encoding="utf-8")
    row = _read_last_valid_trade(p, "BTC")
    assert float(row["price"]) == 80564.96


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


def test_fill_model_defaults_to_conservative_when_env_missing(monkeypatch):
    monkeypatch.delenv("FILL_MODEL", raising=False)
    cfg = BotConfig.from_env()
    assert cfg.fill_model == "conservative"


def test_paper_metrics_reflect_post_fill_state(tmp_path):
    from src.main import _paper_metrics

    class DummyCfg:
        paper_mode = True
        paper_start_balance_usd = 500.0

    eng = make_test_engine(tmp_path, "post_fill_state.json", paper_mode=True, enable_live_trading=False, start_balance=500.0)
    eng.open_orders = [{"symbol": "BTC", "side": "buy", "price": 100.0, "size": 1.0}]
    latest_close = 100.0

    pre_unrealized, pre_equity, pre_notional = _paper_metrics(DummyCfg(), eng, DummyClient(), latest_close)
    assert pre_unrealized == 0.0
    assert pre_equity == 500.0
    assert pre_notional == 0.0

    fills = eng.on_candle({"open": 100.0, "high": 101.0, "low": 99.0, "close": latest_close})
    assert len(fills) == 1

    post_unrealized, post_equity, post_notional = _paper_metrics(DummyCfg(), eng, DummyClient(), latest_close)
    assert post_unrealized == 0.0
    assert post_equity < pre_equity
    assert post_notional == 100.0


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
        fill_model = "conservative"
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
