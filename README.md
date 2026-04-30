# Hyperliquid Adaptive Grid Bot

Adaptive futures grid bot for Hyperliquid with modular Python architecture, risk controls, Telegram monitoring, and paper execution.

## Status
- **Paper/Testnet ready** after local checks pass.
- **Live trading is blocked by default** and remains disabled unless `ENABLE_LIVE_TRADING=true`.
- Use testnet first and validate with your own risk limits.

## Safety Model
- Drawdown and daily loss protections in `RiskManager`.
- Position-notional cap (`MAX_POSITION_NOTIONAL_USD`) can pause new entries and force reduce-only behavior.
- Emergency stop file support:
  - Stop now: `touch state/STOP`
  - Resume: `rm state/STOP`
- Daily risk state (`daily_start_equity`, `current_day`) is persisted so restart does not reset daily-loss protection.

## Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Key Configuration
- Network/auth: `HL_NETWORK`, `HL_PRIVATE_KEY`, `HL_ACCOUNT_ADDRESS`
- Grid: `GRID_LEVELS`, `GRID_SPACING_PCT`, `REGRID_THRESHOLD_PCT`
- Per-trade sizing: `MIN_NOTIONAL_USD`, `MAX_NOTIONAL_PER_TRADE_USD`, `MIN_ORDER_SIZE`, `MAX_ORDER_SIZE`
- Exposure/risk: `MAX_POSITION_NOTIONAL_USD`, `MAX_ONE_DIRECTION_EXPOSURE_PCT`, `MAX_DRAWDOWN_PCT`, `DAILY_LOSS_LIMIT_PCT`
- Execution model: `FILL_MODEL=optimistic|conservative|ohlc_path`
- Recommended paper profile: `FILL_MODEL=conservative` (avoid `optimistic` for realistic fills).
- Runtime guard: `PAPER_MODE=true`, `ENABLE_LIVE_TRADING=false`

## Run
Paper profile:
```bash
./scripts/start_hyperliquid_paper.sh
```

Production script (only after explicit live enable + your own validation):
```bash
./scripts/start_hyperliquid_production.sh
```

## VPS Deployment (systemd example)
```bash
sudo apt update && sudo apt install -y python3-venv git
cd /opt
sudo git clone <your-fork-url> hyperliquid-adaptive-grid-bot
cd hyperliquid-adaptive-grid-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env (keep PAPER_MODE=true first)
PYTHONPATH=. pytest -q
python -m py_compile src/*.py
```

Optional service:
```bash
sudo tee /etc/systemd/system/hyperliquid-grid-bot.service >/dev/null <<'UNIT'
[Unit]
Description=Hyperliquid Adaptive Grid Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/hyperliquid-adaptive-grid-bot
ExecStart=/opt/hyperliquid-adaptive-grid-bot/.venv/bin/python -m src.main
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now hyperliquid-grid-bot
sudo systemctl status hyperliquid-grid-bot
```

## Telegram Monitoring
Set Telegram env vars in your local `.env` (never commit real tokens):

```bash
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_REPORT_INTERVAL_SECONDS=300
TELEGRAM_SEND_STARTUP=true
TELEGRAM_SEND_FILLS=true
TELEGRAM_SEND_RISK_ALERTS=true
TELEGRAM_SEND_PERIODIC_STATUS=true
```

What the bot sends:
- Startup report (once per process start).
- Periodic status report (default every 300s).
- Fill summaries (when fills occur).
- Risk-block alerts (only once per reason change to avoid spam).

⚠️ Never commit your Telegram bot token or chat ID to git.
