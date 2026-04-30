from __future__ import annotations

import logging
import time
import numpy as np
import pandas as pd

from .config import BotConfig
from .execution_engine import ExecutionEngine
from .hyperliquid_client import HyperliquidClient
from .strategy_orchestrator import StrategyOrchestrator
from .telegram_handler import TelegramHandler
from .utils import save_json, setup_logging


logger = logging.getLogger(__name__)


def mock_candles() -> pd.DataFrame:
    base = 60000
    noise = np.random.normal(0, 150, 200).cumsum()
    prices = base + noise
    return pd.DataFrame({"close": prices})


def run() -> None:
    cfg = BotConfig.from_env()
    setup_logging(cfg.log_level)
    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()

    engine = ExecutionEngine(client=client, paper_mode=cfg.env_profile == "paper")
    orchestrator = StrategyOrchestrator(config=cfg, execution_engine=engine)
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)

    while True:
        candles = mock_candles()
        equity = client.get_balance().get("equity", cfg.paper_start_balance_usd)
        status = orchestrator.on_tick(candles, equity=equity, daily_pnl_pct=0.0, symbol=cfg.default_symbol)
        save_json(cfg.state_file, {"status": str(status)})
        tg.send(f"[{cfg.env_profile}] regime={status['regime']} status={status['status']}")
        logger.info("Tick status: %s", status)
        time.sleep(cfg.tick_seconds)


if __name__ == "__main__":
    run()
