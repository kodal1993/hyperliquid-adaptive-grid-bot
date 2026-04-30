from __future__ import annotations

import logging
import time
import pandas as pd

from .config import BotConfig
from .execution_engine import ExecutionEngine
from .hyperliquid_client import HyperliquidClient
from .startup_validation import run_startup_validation
from .strategy_orchestrator import StrategyOrchestrator
from .telegram_handler import TelegramHandler
from .utils import append_csv, save_json, setup_logging

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
    engine = ExecutionEngine(client=client, state_file=cfg.state_file, paper_mode=cfg.paper_mode, enable_live_trading=cfg.enable_live_trading, start_balance=cfg.paper_start_balance_usd)
    orchestrator = StrategyOrchestrator(config=cfg, execution_engine=engine)
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)

    while True:
        candles_raw = client.get_candles(cfg.default_symbol, lookback=200)
        candles = to_df(candles_raw)
        equity = engine.paper.cash if cfg.paper_mode else client.get_balance().get("equity", cfg.paper_start_balance_usd)
        status = orchestrator.on_tick(candles, equity=equity, daily_pnl_pct=0.0, symbol=cfg.default_symbol)
        fills = engine.on_candle(candles.iloc[-1].to_dict())
        for f in fills:
            append_csv("logs/trades.csv", [time.time(), f["symbol"], f["side"], f["price"], f["size"]], ["ts", "symbol", "side", "price", "size"])
        append_csv("logs/equity_curve.csv", [time.time(), engine.paper.cash, engine.paper.realized_pnl], ["ts", "equity", "realized_pnl"])
        save_json(cfg.state_file, {"status": str(status), "paper": engine.paper.__dict__})
        tg.send(f"[{cfg.env_profile}] regime={status.get('regime')} status={status['status']}")
        time.sleep(cfg.tick_seconds)


if __name__ == "__main__":
    run()
