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


def run() -> None:
    cfg = BotConfig.from_env()
    setup_logging(cfg.log_level)
    logger.info("Startup profile=%s paper_mode=%s", cfg.env_profile, cfg.paper_mode)
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

    while True:
        candles_raw = client.get_candles(cfg.default_symbol, lookback=200)
        candles = to_df(candles_raw)
        latest_close = float(candles["close"].iloc[-1])
        unrealized_pnl = engine.unrealized_pnl(latest_close) if cfg.paper_mode else 0.0
        equity = engine.equity(latest_close) if cfg.paper_mode else client.get_balance().get("equity", cfg.paper_start_balance_usd)
        now_day = datetime.now(timezone.utc).date()
        if now_day != current_day:
            current_day = now_day
            daily_start_equity = equity
            engine.paper.current_day = current_day.isoformat()
            engine.paper.daily_start_equity = daily_start_equity
            engine.save_state()
        daily_pnl_pct = 0.0 if daily_start_equity <= 0 else (equity - daily_start_equity) / daily_start_equity
        position_notional = abs(engine.paper.position_size * latest_close) if cfg.paper_mode else 0.0
        status = orchestrator.on_tick(candles, equity=equity, daily_pnl_pct=daily_pnl_pct, symbol=cfg.default_symbol, position_notional=position_notional)
        fills = engine.on_candle(candles.iloc[-1].to_dict())
        for f in fills:
            append_csv("logs/trades.csv", [time.time(), f["symbol"], f["side"], f["price"], f["size"]], ["ts", "symbol", "side", "price", "size"])
        append_csv("logs/equity_curve.csv", [time.time(), equity, engine.paper.cash, engine.paper.realized_pnl, unrealized_pnl, engine.paper.fees_paid], ["ts", "equity", "cash", "realized_pnl", "unrealized_pnl", "fees_paid"])
        engine.paper.current_day = current_day.isoformat()
        engine.paper.daily_start_equity = daily_start_equity
        engine.save_state()
        logger.info(
            "tick symbol=%s price=%.6f regime=%s mode=%s order_size=%.8f order_notional=%.6f position_size=%.8f position_notional=%.6f cash=%.6f realized_pnl=%.6f unrealized_pnl=%.6f fees_paid=%.6f equity=%.6f daily_pnl_pct=%.6f risk_state=%s pause_reason=%s",
            cfg.default_symbol, latest_close, status.get("regime"), status.get("mode"), status.get("order_size", 0.0), status.get("order_notional", 0.0),
            engine.paper.position_size, position_notional, engine.paper.cash, engine.paper.realized_pnl, unrealized_pnl, engine.paper.fees_paid, equity, daily_pnl_pct,
            getattr(status.get("risk"), "reason", ""), status.get("reason", ""),
        )
        tg.send(f"[{cfg.env_profile}] regime={status.get('regime')} status={status['status']}")
        time.sleep(cfg.tick_seconds)


if __name__ == "__main__":
    run()
