# Running BTC + ETH on one account (parallel ETH service)

This adds ETH as a **second, independent service** on the **same Hyperliquid
account** as the existing BTC bot. The proven BTC bot is left untouched; ETH
runs from its own working directory with its own state and lock files.

## Why two processes, not one multi-symbol process

The BTC bot is stable and profitable; a multi-symbol refactor would risk it.
Two processes on the same address still mean **one account**. They share the
Hyperliquid per-address request budget (10k + 1 request per USDC traded), and
coordinate through the budget poll: both poll `userRateLimit`, see the same
headroom, and enter frugal/recovery mode together. To keep combined order-op
consumption safe, the recovery hourly op budget is lowered per process
(`RATE_LIMIT_RECOVERY_MAX_OPS_PER_HOUR=3` each → 6 combined, matching the
single-symbol cap). **Set the BTC instance to 3 as well** when running both.

## szDecimals

Order size rounding (`szDecimals`) is now loaded from the exchange meta at
startup, so ETH (and any symbol) gets correct size steps instead of the
BTC-hardcoded value. No per-symbol code change is needed.

## Deploy steps (VPS)

```bash
# 1. Second working copy for ETH (same repo, separate dir)
git clone <repo-url> /root/hyperliquid-adaptive-grid-bot-eth
cd /root/hyperliquid-adaptive-grid-bot-eth
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. ETH config as .env (same secrets as BTC = same account)
cp config/live-eth.env.example .env
nano .env   # fill HL_PRIVATE_KEY, HL_ACCOUNT_ADDRESS, TELEGRAM_* (same as BTC)

# 3. Install + start the ETH service
sudo cp deploy/systemd/hyperliquid-grid-bot-eth.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperliquid-grid-bot-eth

# 4. Lower the BTC instance's recovery op budget to share the budget safely
#    In the BTC .env: RATE_LIMIT_RECOVERY_MAX_OPS_PER_HOUR=3, then /restartbot
```

## Risk split (BTC-weighted 60/40)

| symbol | order | max position | directional pct |
|--------|-------|--------------|-----------------|
| BTC    | $20   | $60          | (existing)      |
| ETH    | $20   | $40          | 0.60            |

Combined max exposure ~$100 (~31% of $326 at 1x). ETH uses a higher directional
pct (0.60) so the $20 order clears the exposure cap at the smaller $40 ceiling.

## Monitoring

Each service has its own Telegram control bot config / status. Watch the shared
`headroom` from either instance's budget alerts — if it trends down, lower the
order sizes or the per-process op budgets. Stop ETH independently with
`sudo systemctl stop hyperliquid-grid-bot-eth` (the `state/STOP_LIVE_ETH` file
also halts it).
