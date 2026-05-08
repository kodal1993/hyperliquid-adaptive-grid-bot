from __future__ import annotations

import json
import logging
import time
from collections import Counter
import csv
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import BotConfig
from .execution_engine import ExecutionEngine
from .hyperliquid_client import HyperliquidClient
from .startup_validation import run_startup_validation
from .strategy_orchestrator import StrategyOrchestrator
from .telegram_handler import TelegramHandler
from .utils import append_csv, setup_logging

logger = logging.getLogger(__name__)
STATUS_FILE = Path("state/status.json")
TRADES_CSV = Path("logs/trades.csv")
RISK_DECISIONS_CSV = Path("logs/risk_decisions.csv")


def to_df(candles: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"])} for c in candles])


def _paper_metrics(cfg: BotConfig, engine: ExecutionEngine, client: HyperliquidClient, latest_close: float) -> tuple[float, float, float]:
    unrealized_pnl = engine.unrealized_pnl(latest_close) if cfg.paper_mode else 0.0
    equity = engine.equity(latest_close) if cfg.paper_mode else client.get_balance().get("equity", cfg.paper_start_balance_usd)
    position_notional = abs(engine.paper.position_size * latest_close) if cfg.paper_mode else 0.0
    return unrealized_pnl, equity, position_notional


def _write_status(payload: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _derive_risk_state(strategy_status: str, reason: str) -> str:
    hard_block_reasons = {
        "emergency_stop",
        "max_position_notional",
        "max_drawdown",
        "daily_loss_limit",
        "daily_loss",
        "liquidation_risk",
        "liq_distance",
        "one_direction_exposure",
    }
    if strategy_status == "running":
        return "OK"
    if reason in hard_block_reasons:
        return "BLOCKED"
    return "STRATEGY_PAUSED"


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_last_row(csv_path: Path) -> dict:
    if not csv_path.exists():
        return {}
    last: dict = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last = row
    return last


def _risk_block_stats_30m(now_ts: float) -> tuple[int, dict[str, int], str]:
    if not RISK_DECISIONS_CSV.exists():
        return 0, {}, ""
    window_start = now_ts - 30 * 60
    reason_counts: Counter[str] = Counter()
    last_block_reason = ""
    blocked_count = 0
    with RISK_DECISIONS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("decision") != "block":
                continue
            ts_raw = row.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            ts = dt.timestamp()
            if ts < window_start:
                continue
            blocked_count += 1
            reason = row.get("block_reason") or "unknown"
            reason_counts[reason] += 1
            if ts >= window_start:
                last_block_reason = reason
    return blocked_count, dict(reason_counts), last_block_reason


def run() -> None:
    cfg = BotConfig.from_env()
    setup_logging(cfg.log_level)
    logger.info("Startup profile=%s paper_mode=%s fill_model=%s", cfg.env_profile, cfg.paper_mode, cfg.fill_model)
    logger.info("risk_settings max_position_notional_usd=%s soft_exposure_cap_pct=%s hard_exposure_cap_pct=%s absolute_exposure_cap_pct=%s paper_mode=%s enable_live_trading=%s", cfg.max_position_notional_usd, cfg.soft_exposure_cap_pct, cfg.hard_exposure_cap_pct, cfg.absolute_exposure_cap_pct, cfg.paper_mode, cfg.enable_live_trading)
    run_startup_validation(cfg)

    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()
    engine = ExecutionEngine(client=client, state_file=cfg.state_file, paper_mode=cfg.paper_mode, enable_live_trading=cfg.enable_live_trading, start_balance=cfg.paper_start_balance_usd, fill_model=cfg.fill_model, max_position_notional_usd=cfg.max_position_notional_usd, soft_exposure_cap_pct=cfg.soft_exposure_cap_pct, hard_exposure_cap_pct=cfg.hard_exposure_cap_pct, absolute_exposure_cap_pct=cfg.absolute_exposure_cap_pct, allow_position_flip=cfg.allow_position_flip)
    orchestrator = StrategyOrchestrator(config=cfg, execution_engine=engine)
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)
    current_day = datetime.now(timezone.utc).date()
    if engine.paper.current_day:
        current_day = datetime.fromisoformat(engine.paper.current_day).date()
    daily_start_equity = engine.paper.daily_start_equity if engine.paper.daily_start_equity > 0 else cfg.paper_start_balance_usd

    last_telegram_report_ts = 0.0
    last_pause_reason = ""
    last_risk_state_value = ""

    if cfg.telegram_send_startup:
        logger.info("telegram_startup_send_attempted")
        ok = tg.send("\n".join(["🚀 Hyperliquid Grid Bot Started", f"Mode: {'PAPER' if cfg.paper_mode else 'LIVE'}", f"Network: {cfg.hl_network}", f"Symbol: {cfg.default_symbol}", f"Fill Model: {cfg.fill_model}", f"Live Trading: {'ENABLED' if cfg.enable_live_trading else 'DISABLED'}", f"Profile: {cfg.env_profile}"]))
        if ok:
            logger.info("telegram_startup_send_ok")
        else:
            logger.warning("telegram_startup_send_failed")

    while True:
        candles = to_df(client.get_candles(cfg.default_symbol, lookback=200))
        latest_close = float(candles["close"].iloc[-1])
        unrealized_pnl, equity, position_notional = _paper_metrics(cfg, engine, client, latest_close)
        now_day = datetime.now(timezone.utc).date()
        if now_day != current_day:
            current_day = now_day
            daily_start_equity = equity
            engine.paper.current_day = current_day.isoformat()
            engine.paper.daily_start_equity = daily_start_equity
            engine.save_state()
        daily_pnl_pct = 0.0 if daily_start_equity <= 0 else (equity - daily_start_equity) / daily_start_equity

        status = orchestrator.on_tick(candles, equity=equity, daily_pnl_pct=daily_pnl_pct, symbol=cfg.default_symbol, position_notional=position_notional)
        risk = status.get("risk")
        reason = status.get("reason") or (getattr(risk, "reason", "") if risk else "")
        strategy_status = status.get("status", "unknown")
        risk_state = _derive_risk_state(strategy_status, reason)
        mode = status.get("mode", "reduce_only" if reason == "reduce_only_requested" else "neutral")

        fills = engine.on_candle(candles.iloc[-1].to_dict(), regime=status.get("regime", "unknown"), mode=mode, risk_state=risk_state, pause_reason=reason, strategy_status=strategy_status)
        trade_events = engine.consume_trade_log()
        unrealized_pnl, equity, position_notional = _paper_metrics(cfg, engine, client, latest_close)

        for tr in trade_events:
            logger.info("trade_event side=%s symbol=%s price=%.6f qty=%.8f", tr["side"], tr["symbol"], tr["price"], tr["qty"])
            if cfg.telegram_send_fills:
                tg.send("\n".join([
                    "✅ Trade / Fill",
                    f"side: {tr['side']}",
                    f"symbol: {tr['symbol']}",
                    f"price: {tr['price']:.6f}",
                    f"qty: {tr['qty']:.8f}",
                    f"notional: {tr['notional']:.4f}",
                    f"fee: {tr['fee']:.6f}",
                    f"realized_pnl_delta: {tr['realized_pnl_delta']:.6f}",
                    f"position_before: {tr['position_before']:.8f}",
                    f"position_after: {tr['position_after']:.8f}",
                    f"cash_after: {tr['cash']:.6f}",
                    f"equity: {tr['equity']:.6f}",
                    f"regime: {tr['regime']}",
                    f"mode: {tr['mode']}",
                    f"risk_state: {tr['risk_state']}",
                    f"pause_reason: {tr['pause_reason'] or 'none'}",
                ]))

        if cfg.telegram_send_risk_alerts and (risk_state != last_risk_state_value or reason != last_pause_reason):
            tg.send(f"🛡 Risk update\nrisk_state: {last_risk_state_value or 'INIT'} -> {risk_state}\npause_reason: {last_pause_reason or 'none'} -> {reason or 'none'}")
            last_risk_state_value = risk_state
            last_pause_reason = reason

        append_csv("logs/equity_curve.csv", [time.time(), equity, engine.paper.cash, engine.paper.realized_pnl, unrealized_pnl, engine.paper.fees_paid], ["ts", "equity", "cash", "realized_pnl", "unrealized_pnl", "fees_paid"])

        status_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": cfg.default_symbol,
            "price": latest_close,
            "regime": status.get("regime", "unknown"),
            "mode": mode,
            "order_size": status.get("order_size", 0.0),
            "order_notional": status.get("order_notional", 0.0),
            "position_size": engine.paper.position_size,
            "position_notional": position_notional,
            "cash": engine.paper.cash,
            "realized_pnl": engine.paper.realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "fees_paid": engine.paper.fees_paid,
            "equity": equity,
            "daily_pnl_pct": daily_pnl_pct,
            "risk_state": risk_state,
            "strategy_status": strategy_status,
            "pause_reason": reason,
        }

        last_trade = _read_last_row(TRADES_CSV)
        blocked_30m, blocked_by_reason, last_block_reason = _risk_block_stats_30m(now_ts=time.time())
        position_side = "FLAT"
        if engine.paper.position_size > 0:
            position_side = "LONG"
        elif engine.paper.position_size < 0:
            position_side = "SHORT"
        status_payload.update(
            {
                "position_side": position_side,
                "mark_price": latest_close,
                "last_trade_price": _safe_float(last_trade.get("price")),
                "last_trade_qty": _safe_float(last_trade.get("qty")),
                "last_trade_notional": _safe_float(last_trade.get("notional")),
                "last_trade_fee": _safe_float(last_trade.get("fee")),
                "last_trade_realized_pnl": _safe_float(last_trade.get("realized_pnl_delta")),
                "last_trade_reason": last_trade.get("reason", ""),
                "last_trade_side": last_trade.get("side", ""),
                "blocked_risk_decisions_30m": blocked_30m,
                "blocked_risk_decisions_by_reason": blocked_by_reason,
                "last_block_reason": last_block_reason,
                "allowed_to_trade": risk_state == "OK",
            }
        )
        _write_status(status_payload)

        now_ts = time.time()
        if cfg.telegram_send_periodic_status and (now_ts - last_telegram_report_ts) >= cfg.telegram_report_interval_seconds:
            tg.send_status_report({**status_payload, "status": status.get("status", "unknown"), "network": cfg.hl_network, "mode_name": "PAPER" if cfg.paper_mode else "LIVE", "grid_mode": mode, "fill_model": cfg.fill_model, "risk_reason": reason or "none", "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")})
            last_telegram_report_ts = now_ts

        engine.paper.current_day = current_day.isoformat()
        engine.paper.daily_start_equity = daily_start_equity
        engine.save_state()
        time.sleep(cfg.tick_seconds)


if __name__ == "__main__":
    run()
