from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from .config import BotConfig
from .execution_engine import ExecutionEngine
from .hyperliquid_client import HyperliquidClient
from .startup_validation import run_startup_validation
from .strategy_orchestrator import StrategyOrchestrator
from .telegram_handler import TelegramHandler
from .utils import append_csv, setup_logging

logger = logging.getLogger(__name__)


def to_df(candles: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"])} for c in candles])


def _paper_metrics(cfg: BotConfig, engine: ExecutionEngine, client: HyperliquidClient, latest_close: float) -> tuple[float, float, float]:
    unrealized_pnl = engine.unrealized_pnl(latest_close) if cfg.paper_mode else 0.0
    equity = engine.equity(latest_close) if cfg.paper_mode else client.get_balance().get("equity", cfg.paper_start_balance_usd)
    position_notional = abs(engine.paper.position_size * latest_close) if cfg.paper_mode else 0.0
    return unrealized_pnl, equity, position_notional


def run() -> None:
    cfg = BotConfig.from_env()
    setup_logging(cfg.log_level)
    logger.info("Startup profile=%s paper_mode=%s fill_model=%s", cfg.env_profile, cfg.paper_mode, cfg.fill_model)
    run_startup_validation(cfg)

    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()
    engine = ExecutionEngine(client=client, state_file=cfg.state_file, paper_mode=cfg.paper_mode, enable_live_trading=cfg.enable_live_trading, start_balance=cfg.paper_start_balance_usd, fill_model=cfg.fill_model)
    orchestrator = StrategyOrchestrator(config=cfg, execution_engine=engine)
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)
    current_day = datetime.now(timezone.utc).date()
    if engine.paper.current_day:
        current_day = datetime.fromisoformat(engine.paper.current_day).date()
    daily_start_equity = engine.paper.daily_start_equity if engine.paper.daily_start_equity > 0 else cfg.paper_start_balance_usd

    last_telegram_report_ts = 0.0
    last_risk_reason_sent = ""
    startup_report_sent = False
    first_fill_sent = False
    stuck_since_ts: float | None = None
    stuck_alert_sent = False

    if cfg.telegram_send_startup and not startup_report_sent:
        tg.send(
            "\n".join(
                [
                    "🚀 Hyperliquid Grid Bot Started",
                    f"Mode: {'PAPER' if cfg.paper_mode else 'LIVE'}",
                    f"Network: {cfg.hl_network}",
                    f"Symbol: {cfg.default_symbol}",
                    f"Fill Model: {cfg.fill_model}",
                    f"Live Trading: {'ENABLED' if cfg.enable_live_trading else 'DISABLED'}",
                    f"Profile: {cfg.env_profile}",
                ]
            )
        )
        startup_report_sent = True

    while True:
        candles_raw = client.get_candles(cfg.default_symbol, lookback=200)
        candles = to_df(candles_raw)
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
        fills = engine.on_candle(candles.iloc[-1].to_dict())
        unrealized_pnl, equity, position_notional = _paper_metrics(cfg, engine, client, latest_close)

        for f in fills:
            append_csv("logs/trades.csv", [time.time(), f["symbol"], f["side"], f["price"], f["size"]], ["ts", "symbol", "side", "price", "size"])
        append_csv("logs/equity_curve.csv", [time.time(), equity, engine.paper.cash, engine.paper.realized_pnl, unrealized_pnl, engine.paper.fees_paid], ["ts", "equity", "cash", "realized_pnl", "unrealized_pnl", "fees_paid"])
        engine.paper.current_day = current_day.isoformat()
        engine.paper.daily_start_equity = daily_start_equity
        engine.save_state()

        stuck_threshold = cfg.max_position_notional_usd * 0.5
        if position_notional > stuck_threshold:
            if stuck_since_ts is None:
                stuck_since_ts = time.time()
                stuck_alert_sent = False
            stuck_minutes = (time.time() - stuck_since_ts) / 60.0
            if stuck_minutes >= cfg.stuck_position_minutes and not stuck_alert_sent:
                logger.warning("stuck_inventory symbol=%s position_notional=%.2f threshold=%.2f minutes=%.2f", cfg.default_symbol, position_notional, stuck_threshold, stuck_minutes)
                tg.send("\n".join(["⚠️ stuck_inventory", f"Symbol: {cfg.default_symbol}", f"Position Notional: ${position_notional:,.2f}", f"Threshold: ${stuck_threshold:,.2f}", f"Minutes: {stuck_minutes:.1f}"]))
                stuck_alert_sent = True
        else:
            stuck_since_ts = None
            stuck_alert_sent = False

        risk = status.get("risk")
        reason = status.get("reason") or (getattr(risk, "reason", "") if risk else "")
        risk_state = "OK" if status.get("status") == "running" else "BLOCKED"
        mode = status.get("mode", "reduce_only" if reason == "reduce_only_requested" else "neutral")

        if cfg.telegram_send_risk_alerts and reason in {"emergency_stop", "reduce_only_requested", "daily_loss", "max_drawdown", "max_position_notional", "neutral_blocked_in_trend"} and reason != last_risk_reason_sent:
            tg.send(
                "\n".join(
                    [
                        "🚨 Risk Block Triggered",
                        f"Reason: {reason}",
                        "Action: Entry orders canceled / reduce-only requested" if reason in {"reduce_only_requested", "max_position_notional"} else "Action: Trading paused",
                        f"Position Notional: ${position_notional:,.2f}",
                        f"Equity: ${equity:,.2f}",
                    ]
                )
            )
            last_risk_reason_sent = reason
        elif not reason:
            last_risk_reason_sent = ""

        if fills and cfg.telegram_send_fills:
            last_fill = fills[-1]
            tg.send(
                "\n".join(
                    [
                        "✅ Paper Fill",
                        f"Fills: {len(fills)}",
                        f"Side: {str(last_fill['side']).upper()}",
                        f"Size: {last_fill['size']:.6f} {cfg.default_symbol}",
                        f"Price: ${last_fill['price']:,.2f}",
                        f"Notional: ${last_fill['price'] * last_fill['size']:,.2f}",
                        f"Realized PnL: ${engine.paper.realized_pnl:,.2f}",
                        f"Position: {engine.paper.position_size:.6f} {cfg.default_symbol}",
                        f"Equity: ${equity:,.2f}",
                    ]
                )
            )
            if not first_fill_sent:
                first_fill_sent = True

        now_ts = time.time()
        if cfg.telegram_send_periodic_status and (now_ts - last_telegram_report_ts) >= cfg.telegram_report_interval_seconds:
            last_fill_text = "none"
            if fills:
                lf = fills[-1]
                last_fill_text = f"{str(lf['side']).lower()} {lf['size']:.6f} @ ${lf['price']:,.2f}"
            tg.send_status_report(
                {
                    "status": status.get("status", "unknown"),
                    "network": cfg.hl_network,
                    "mode_name": "PAPER" if cfg.paper_mode else "LIVE",
                    "symbol": cfg.default_symbol,
                    "regime": status.get("regime", "n/a"),
                    "grid_mode": mode,
                    "fill_model": cfg.fill_model,
                    "equity": equity,
                    "cash": engine.paper.cash,
                    "realized_pnl": engine.paper.realized_pnl,
                    "unrealized_pnl": unrealized_pnl,
                    "fees_paid": engine.paper.fees_paid,
                    "daily_pnl_pct": daily_pnl_pct,
                    "position_size": engine.paper.position_size,
                    "position_notional": position_notional,
                    "avg_entry": engine.paper.avg_entry,
                    "mark_price": latest_close,
                    "open_orders": len(engine.open_orders),
                    "last_regrid": "yes" if status.get("orders", {}).get("placed", 0) else "no",
                    "order_size": status.get("order_size"),
                    "order_notional": status.get("order_notional"),
                    "grid_levels": cfg.grid_levels,
                    "grid_spacing_pct": cfg.grid_spacing_pct,
                    "max_position": cfg.max_position_notional_usd,
                    "drawdown": getattr(risk, "drawdown", 0.0) if risk else 0.0,
                    "daily_loss_limit_pct": cfg.daily_loss_limit_pct,
                    "risk_state": risk_state,
                    "risk_reason": reason or "none",
                    "fills_count": len(fills),
                    "last_fill": last_fill_text,
                    "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                }
            )
            last_telegram_report_ts = now_ts

        logger.info(
            "tick symbol=%s price=%.6f regime=%s mode=%s order_size=%.8f order_notional=%.6f position_size=%.8f position_notional=%.6f cash=%.6f realized_pnl=%.6f unrealized_pnl=%.6f fees_paid=%.6f equity=%.6f daily_pnl_pct=%.6f risk_state=%s pause_reason=%s",
            cfg.default_symbol, latest_close, status.get("regime"), status.get("mode"), status.get("order_size", 0.0), status.get("order_notional", 0.0),
            engine.paper.position_size, position_notional, engine.paper.cash, engine.paper.realized_pnl, unrealized_pnl, engine.paper.fees_paid, equity, daily_pnl_pct,
            getattr(status.get("risk"), "reason", ""), status.get("reason", ""),
        )
        time.sleep(cfg.tick_seconds)


if __name__ == "__main__":
    run()
