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

## One Telegram controller for both symbols

Run a **single** control bot that manages BTC and ETH (one token, one chat).
The two trading services only **send** fills/status (no command polling), and
the control bot is the sole command consumer.

1. On both trading instances set `TELEGRAM_ENABLE_COMMANDS=false` (they still
   send fills/status). This avoids two processes fighting over Telegram
   `getUpdates` with the same token.
2. Run the controller itself as its own long-lived systemd service — see
   `deploy/systemd/hyperliquid-telegram-control.service`:
   ```bash
   sudo cp deploy/systemd/hyperliquid-telegram-control.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now hyperliquid-telegram-control
   ```
3. Point the control bot at both instances via env (in the controller's
   working dir `.env`, e.g. the BTC instance's):
   ```
   TELEGRAM_CONTROL_INSTANCES=BTC:hyperliquid-grid-bot-live:/root/hyperliquid-adaptive-grid-bot-live:BTC,ETH:hyperliquid-grid-bot-eth:/root/hyperliquid-adaptive-grid-bot-eth:ETH
   ```
   Format per instance: `label:systemd-service:working-dir:symbol`.
4. Also set `TELEGRAM_CONTROL_SELF_SERVICE=hyperliquid-telegram-control` (or
   whatever you named the unit in step 2) so `/pull` restarts the controller
   process itself, not just the trading bots.

Then `/status`, `/orders`, `/position`, `/trades`, `/performance` aggregate both
symbols (labeled sections), and the inline menu shows per-symbol Stop/Start/
Cancel/Close buttons. Text commands accept an optional symbol, e.g.
`/stopbot eth` (no argument = all instances). With `TELEGRAM_CONTROL_INSTANCES`
unset the controller behaves exactly as before (single instance).

`/pull` (or `/update`) updates **every** instance: it runs `git pull --ff-only`
in each instance's working dir (and `pip install` if `requirements.txt`
changed), then restarts that instance's service — reported per instance. A
failed pull skips that instance's restart (no half-updated restart). Finally,
if `TELEGRAM_CONTROL_SELF_SERVICE` is set, it restarts the controller's own
service so the controller process (which holds `scripts/telegram_control_bot.py`
in memory) picks up the pulled code too — without this, `/pull` updates the
trading bots but the controller keeps running its old code (e.g. it won't
show a newly added ETH instance in `/status` until it's restarted manually).

## Monitoring

Watch the shared `headroom` from the budget alerts — if it trends down, lower
the order sizes or the per-process op budgets. Stop ETH independently with
`sudo systemctl stop hyperliquid-grid-bot-eth` (or `/stopbot eth`); the
`state/STOP_LIVE_ETH` file also halts it.
