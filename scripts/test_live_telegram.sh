#!/usr/bin/env bash
set -euo pipefail

cd "${1:-/root/hyperliquid-adaptive-grid-bot-live}"

ENV_PROFILE=live .venv/bin/python - <<'PY'
from src.config import BotConfig
from src.telegram_handler import TelegramHandler

cfg = BotConfig.from_env()
tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)

msg = (
    "🧪 LIVE Telegram test\n"
    "Mode: LIVE CONFIG\n"
    f"Network: {cfg.hl_network}\n"
    f"Symbol: {cfg.default_symbol}\n"
    f"Execution mode: live_only\n"
    f"Live trading: {cfg.enable_live_trading}\n"
    f"Max trade: {cfg.max_notional_per_trade_usd} USD\n"
    f"Max position: {cfg.max_position_notional_usd} USD"
)

ok = tg.send(msg)
print("telegram_test_ok:", ok)
if not ok:
    print("last_error:", tg.last_error)
    raise SystemExit(1)
PY
