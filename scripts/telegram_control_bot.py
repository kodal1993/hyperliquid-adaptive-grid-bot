#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

from src.config import BotConfig
from src.hyperliquid_client import HyperliquidClient

SERVICE_NAME = os.getenv("TELEGRAM_CONTROL_SERVICE_NAME", "hyperliquid-grid-bot-live")
STATUS_FILE = Path(os.getenv("TELEGRAM_CONTROL_STATUS_FILE", "data/status.json"))
TRADES_FILE = Path(os.getenv("TELEGRAM_CONTROL_TRADES_FILE", "logs/trades.jsonl"))
OFFSET_FILE = Path(os.getenv("TELEGRAM_CONTROL_OFFSET_FILE", "state/telegram_control.offset"))
POLL_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_CONTROL_POLL_TIMEOUT_SECONDS", "25"))
POLL_SLEEP_SECONDS = float(os.getenv("TELEGRAM_CONTROL_POLL_SLEEP_SECONDS", "1"))
PULL_RESTART_SCRIPT = "/usr/local/bin/hyperliquid_pull_restart.sh"
PULL_RESTART_TIMEOUT_SECONDS = 60
MAX_MESSAGE_LEN = 3800
logger = logging.getLogger(__name__)
BOT_COMMANDS = [
    {"command": "menu", "description": "Inline vezérlő menü"},
    {"command": "status", "description": "Bot + exchange státusz"},
    {"command": "startbot", "description": "Live bot service indítása"},
    {"command": "stopbot", "description": "Live bot service leállítása"},
    {"command": "restartbot", "description": "Live bot service újraindítása"},
    {"command": "pull", "description": "Git pull + LIVE bot újraindítás"},
    {"command": "update", "description": "Git pull + LIVE bot újraindítás"},
    {"command": "cancelall", "description": "Stop + összes open order törlése"},
    {"command": "panic", "description": "Vészstop: stop + cancel + snapshot"},
    {"command": "orders", "description": "Open orderek lekérése"},
    {"command": "position", "description": "Aktuális pozíció lekérése"},
    {"command": "trades", "description": "Utolsó trade-ek"},
    {"command": "performance", "description": "Teljesítmény statisztika"},
    {"command": "closeposition", "description": "Pozíció zárása"},
    {"command": "help", "description": "Parancslista"},
]


def load_config() -> BotConfig:
    return BotConfig.from_env()


def allowed_chat_ids(cfg: BotConfig) -> set[str]:
    raw = os.getenv("TELEGRAM_CONTROL_ALLOWED_CHAT_IDS", "") or cfg.telegram_chat_id
    return {item.strip() for item in raw.split(",") if item.strip()}


def telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def register_bot_commands(token: str) -> None:
    requests.post(telegram_api(token, "setMyCommands"), json={"commands": BOT_COMMANDS}, timeout=15).raise_for_status()


def main_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Status", "callback_data": "cmd:status"},
                {"text": "📌 Position", "callback_data": "cmd:position"},
            ],
            [
                {"text": "📋 Orders", "callback_data": "cmd:orders"},
                {"text": "📈 Trades", "callback_data": "cmd:trades"},
            ],
            [{"text": "📉 Performance", "callback_data": "cmd:performance"}],
            [
                {"text": "🛑 Stop bot", "callback_data": "confirm:stopbot"},
                {"text": "▶️ Start bot", "callback_data": "cmd:startbot"},
            ],
            [
                {"text": "❌ Cancel orders", "callback_data": "confirm:cancelall"},
                {"text": "🚪 Close position", "callback_data": "confirm:closeposition"},
            ],
            [{"text": "🔄 Refresh", "callback_data": "cmd:menu"}],
        ]
    }


def confirm_keyboard(action: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Confirm", "callback_data": f"do:{action}"},
                {"text": "❌ Cancel", "callback_data": "cmd:menu"},
            ]
        ]
    }


def send_message(token: str, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
    chunks = [text[i : i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)] or [text]
    for idx, chunk in enumerate(chunks):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
        if reply_markup is not None and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(telegram_api(token, "sendMessage"), json=payload, timeout=15).raise_for_status()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            return False
    return True


def answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(
            telegram_api(token, "answerCallbackQuery"),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        ).raise_for_status()
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        return


def menu_text() -> str:
    return """🤖 HYPERLIQUID LIVE BOT
Execution: LIVE

Choose action:"""


def run_cmd(args: list[str], *, timeout: int = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output.strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"



def redact_sensitive_output(text: str) -> str:
    redacted = text
    redacted = re.sub(
        r"(?i)(telegram[_-]?(?:bot[_-]?)?token|hl[_-]?private[_-]?key|private[_-]?key|secret|password|passphrase|api[_-]?key)\s*([:=])\s*([^\s]+)",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?i)(bot)[0-9]{5,}:[A-Za-z0-9_-]{20,}", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)0x[a-f0-9]{64}", "0x[REDACTED]", redacted)
    return redacted


def summarize_command_output(stdout: str, stderr: str, *, max_chars: int = 3000) -> str:
    output = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part)
    if not output:
        return "(nincs stdout/stderr)"
    output = redact_sensitive_output(output)
    if len(output) > max_chars:
        output = output[:max_chars].rstrip() + "\n... (truncated)"
    return output


def run_pull_restart_script() -> tuple[int, str]:
    logger.info("Telegram /pull execution starting script=%s timeout=%ss", PULL_RESTART_SCRIPT, PULL_RESTART_TIMEOUT_SECONDS)
    try:
        completed = subprocess.run(
            [PULL_RESTART_SCRIPT],
            capture_output=True,
            text=True,
            timeout=PULL_RESTART_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        summary = summarize_command_output(exc.stdout or "", exc.stderr or "")
        logger.warning("Telegram /pull execution timed out script=%s timeout=%ss", PULL_RESTART_SCRIPT, PULL_RESTART_TIMEOUT_SECONDS)
        return 124, f"❌ Git pull / restart timeout ({PULL_RESTART_TIMEOUT_SECONDS}s).\n{summary}"
    except Exception as exc:
        logger.exception("Telegram /pull execution failed before completion script=%s", PULL_RESTART_SCRIPT)
        return 1, f"❌ Git pull / restart hiba.\n{type(exc).__name__}: {redact_sensitive_output(str(exc))}"

    summary = summarize_command_output(completed.stdout or "", completed.stderr or "")
    logger.info("Telegram /pull execution finished script=%s rc=%s", PULL_RESTART_SCRIPT, completed.returncode)
    if completed.returncode == 0:
        return completed.returncode, "✅ Git pull kész, LIVE bot újraindítva."
    return completed.returncode, f"❌ Git pull / restart hiba (rc={completed.returncode}).\n{summary}"

def fmt_money(value: Any, digits: int = 2) -> str:
    try:
        return f"${float(value):,.{digits}f}"
    except Exception:
        return "n/a"


def fmt_num(value: Any, digits: int = 6) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "n/a"


def fmt_pct(value: Any, digits: int = 3) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except Exception:
        return "n/a"


def fmt_price(value: Any) -> str:
    try:
        return f"${float(value):,.0f}"
    except Exception:
        return "n/a"


def short_side_icon(side: str) -> str:
    s = str(side or "").lower()
    if s in {"b", "buy", "long"}:
        return "🟢 LONG/BUY"
    if s in {"a", "ask", "sell", "short"}:
        return "🔴 SHORT/SELL"
    return "⚪ N/A"


def service_state() -> tuple[str, str]:
    active_rc, active = run_cmd(["systemctl", "is-active", SERVICE_NAME], timeout=10)
    enabled_rc, enabled = run_cmd(["systemctl", "is-enabled", SERVICE_NAME], timeout=10)
    return active if active_rc == 0 else active or "unknown", enabled if enabled_rc == 0 else enabled or "unknown"


def read_status() -> dict[str, Any]:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def make_client(cfg: BotConfig) -> HyperliquidClient:
    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()
    return client


def get_exchange_data(cfg: BotConfig) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
    client = make_client(cfg)
    state = client.get_user_state()
    orders = client.get_open_orders(cfg.default_symbol)
    position = client.get_position(cfg.default_symbol)
    return state, orders, position


def format_orders(orders: list[dict[str, Any]], symbol: str) -> str:
    lines = ["📋 OPEN ORDERS", "Execution: LIVE", f"Symbol: {symbol}", f"Count: {len(orders)}", ""]
    if not orders:
        lines.append("✅ Nincs nyitott order.")
        return "\n".join(lines)

    for idx, order in enumerate(orders, 1):
        side = order.get("side") or order.get("dir") or order.get("direction")
        price = order.get("limitPx") or order.get("limit_px") or order.get("price")
        size = order.get("sz") or order.get("size") or order.get("origSz") or order.get("orig_size")
        oid = order.get("oid") or order.get("orderId") or "n/a"
        try:
            notional = float(price) * float(size)
        except Exception:
            notional = None
        lines.extend(
            [
                f"#{idx} {short_side_icon(str(side))}",
                f"├ Ár: {fmt_price(price)}",
                f"├ Méret: {fmt_num(size, 8)} BTC",
                f"├ Érték: {fmt_money(notional)}",
                f"└ Order ID: {oid}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def format_position(position: Any, symbol: str) -> str:
    lines = ["📌 POSITION", "Execution: LIVE", f"Symbol: {symbol}", ""]
    if not position:
        lines.append("✅ FLAT — nincs nyitott pozíció.")
        return "\n".join(lines)

    if isinstance(position, dict):
        size = position.get("szi") or position.get("size") or position.get("position_size") or 0
        entry = position.get("entryPx") or position.get("entry_price") or position.get("avg_entry")
        value = position.get("positionValue") or position.get("notional") or position.get("position_notional")
        upnl = position.get("unrealizedPnl") or position.get("unrealized_pnl")
        liq = position.get("liquidationPx") or position.get("liquidation_price")
        lev = position.get("leverage")
        margin = position.get("marginUsed") or position.get("margin_used")
        try:
            side = "SHORT" if float(size) < 0 else "LONG" if float(size) > 0 else "FLAT"
        except Exception:
            side = "N/A"
        lines.extend(
            [
                f"Irány: {short_side_icon(side)}",
                f"Méret: {fmt_num(size, 8)} BTC",
                f"Notional: {fmt_money(value)}",
                f"Entry: {fmt_price(entry)}",
                f"Unrealized PnL: {fmt_money(upnl, 4)}",
                f"Likvidáció: {fmt_price(liq)}",
                f"Margin used: {fmt_money(margin)}",
                f"Leverage: {json.dumps(lev, ensure_ascii=False) if lev is not None else 'n/a'}",
            ]
        )
        return "\n".join(lines)

    return "📌 POSITION\nExecution: LIVE\n" + json.dumps(position, indent=2, ensure_ascii=False)[:2500]


def format_account(state: dict[str, Any]) -> str:
    margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
    return "\n".join(
        [
            "🏦 ACCOUNT",
            "Execution: LIVE",
            f"Equity: {fmt_money(margin.get('accountValue'))}",
            f"Withdrawable: {fmt_money(state.get('withdrawable') if isinstance(state, dict) else None)}",
            f"Total notional pos: {fmt_money(margin.get('totalNtlPos'))}",
            f"Margin used: {fmt_money(margin.get('totalMarginUsed'))}",
        ]
    )


def read_trades(limit: int | None = None) -> list[dict[str, Any]]:
    if not TRADES_FILE.exists():
        return []
    trades: list[dict[str, Any]] = []
    try:
        with TRADES_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    trades.append(item)
    except OSError:
        return []
    if limit is not None:
        return trades[-limit:]
    return trades


def _trade_float(trade: dict[str, Any], key: str) -> float:
    try:
        return float(trade.get(key, 0.0) or 0.0)
    except Exception:
        return 0.0


def format_trades(limit: int = 10) -> str:
    trades = read_trades(limit)
    lines = ["📈 TRADES", f"Execution: LIVE", f"Source: {TRADES_FILE}", f"Last: {len(trades)}", ""]
    if not trades:
        lines.append("No trades found.")
        return "\n".join(lines)

    for idx, trade in enumerate(reversed(trades), 1):
        side = str(trade.get("side") or "n/a").upper()
        ts = str(trade.get("timestamp") or "n/a")
        symbol = str(trade.get("symbol") or "n/a")
        lines.extend(
            [
                f"#{idx} {short_side_icon(side)} {symbol}",
                f"├ Time: {ts}",
                f"├ Price: {fmt_price(trade.get('price'))}",
                f"├ Qty: {fmt_num(trade.get('qty'), 8)}",
                f"├ Notional: {fmt_money(trade.get('notional'))}",
                f"├ Fee: {fmt_money(trade.get('fee'), 4)}",
                f"└ Realized Δ: {fmt_money(trade.get('realized_pnl_delta'), 4)}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def format_performance() -> str:
    trades = read_trades()
    lines = ["📉 PERFORMANCE", "Execution: LIVE", f"Source: {TRADES_FILE}", ""]
    if not trades:
        lines.append("No trades found.")
        return "\n".join(lines)

    total_notional = sum(abs(_trade_float(t, "notional")) for t in trades)
    total_fees = sum(abs(_trade_float(t, "fee")) for t in trades)
    realized_delta = sum(_trade_float(t, "realized_pnl_delta") for t in trades)
    net_pnls = [_trade_float(t, "realized_pnl_delta") - abs(_trade_float(t, "fee")) for t in trades]
    net_pnl = sum(net_pnls)
    gross_pnl_before_fees = net_pnl + total_fees
    gross_profit = sum(x for x in net_pnls if x > 0)
    gross_loss = sum(abs(x) for x in net_pnls if x < 0)
    profit_factor_after_fees = gross_profit / gross_loss if gross_loss > 1e-12 else (gross_profit if gross_profit > 0 else 0.0)
    dust_trade_count = sum(1 for t in trades if str(t.get("dust_fill", "")).lower() == "true" or (0.0 < abs(_trade_float(t, "notional")) < 2.0))
    avg_net_profit_per_closed_trade = net_pnl / max(sum(1 for t in trades if abs(_trade_float(t, "realized_pnl_delta")) > 1e-12), 1)
    avg_fee_per_trade = total_fees / max(len(trades), 1)
    fee_to_gross_profit_ratio = total_fees / gross_profit if gross_profit > 1e-12 else 0.0
    wins = sum(1 for t in trades if _trade_float(t, "realized_pnl_delta") > 0)
    losses = sum(1 for t in trades if _trade_float(t, "realized_pnl_delta") < 0)
    buys = sum(1 for t in trades if str(t.get("side", "")).lower() in {"buy", "b"})
    sells = sum(1 for t in trades if str(t.get("side", "")).lower() in {"sell", "a", "ask"})
    first_ts = str(trades[0].get("timestamp") or "n/a")
    last_ts = str(trades[-1].get("timestamp") or "n/a")
    win_rate = wins / max(wins + losses, 1)
    last_equity = trades[-1].get("equity")

    lines.extend(
        [
            f"Trades: {len(trades)} | BUY {buys} / SELL {sells}",
            f"Win/Loss: {wins}/{losses} ({fmt_pct(win_rate, 2)})",
            f"Volume: {fmt_money(total_notional)}",
            f"Fees: {fmt_money(total_fees, 4)}",
            f"Realized PnL Δ: {fmt_money(realized_delta, 4)}",
            f"Gross PnL before fees: {fmt_money(gross_pnl_before_fees, 4)}",
            f"Net PnL: {fmt_money(net_pnl, 4)}",
            f"Fee/gross profit ratio: {fmt_pct(fee_to_gross_profit_ratio, 2)}",
            f"Dust trades: {dust_trade_count}",
            f"Avg net/closed trade: {fmt_money(avg_net_profit_per_closed_trade, 4)}",
            f"Avg fee/trade: {fmt_money(avg_fee_per_trade, 4)}",
            f"Profit factor after fees: {profit_factor_after_fees:.2f}",
            f"Last equity: {fmt_money(last_equity)}",
            "",
            f"First trade: {first_ts}",
            f"Last trade: {last_ts}",
        ]
    )
    return "\n".join(lines)


def compact_status() -> str:
    status = read_status()
    if not status:
        return f"⚠️ Nincs status fájl: {STATUS_FILE}"

    active, enabled = service_state()
    risk = status.get("risk_state")
    risk_icon = "🟢" if risk == "OK" else "🟡" if risk else "⚪"
    position_side = status.get("position_side") or "FLAT"
    daily_pnl = status.get("daily_pnl")
    daily_pnl_pct = status.get("daily_pnl_pct")

    return "\n".join(
        [
            "📊 HYPERLIQUID LIVE BOT",
            "Execution: LIVE",
            f"Service: {'🟢' if active == 'active' else '🔴'} {active} | enabled: {enabled}",
            "",
            "⚙️ STRATÉGIA",
            f"Állapot: {status.get('status')}",
            f"Piac: {status.get('symbol')} @ {fmt_price(status.get('price'))}",
            f"Regime/mode: {status.get('regime')} / {status.get('mode')}",
            f"Risk: {risk_icon} {risk}",
            f"Pause: {status.get('pause_reason') or 'none'}",
            f"Last error: {status.get('last_error') or 'none'}",
            "",
            "🏦 SZÁMLA",
            f"Equity: {fmt_money(status.get('equity'))}",
            f"Daily PnL: {fmt_money(daily_pnl, 4)} ({fmt_pct(daily_pnl_pct)})",
            f"Realized: {fmt_money(status.get('realized_pnl'), 4)}",
            f"Unrealized: {fmt_money(status.get('unrealized_pnl'), 4)}",
            "",
            "📌 POZÍCIÓ",
            f"Side: {short_side_icon(str(position_side))}",
            f"Size: {fmt_num(status.get('position_size'), 8)} BTC",
            f"Notional: {fmt_money(status.get('position_notional'))}",
            "",
            "📋 GRID",
            f"Open orders: {status.get('open_orders')} | BUY {status.get('active_buy_orders')} / SELL {status.get('active_sell_orders')}",
            f"Nearest BUY: {fmt_price(status.get('nearest_buy_price'))}",
            f"Nearest SELL: {fmt_price(status.get('nearest_sell_price'))}",
            "",
            "🕒 UTOLSÓ TRADE",
            f"Side: {status.get('last_trade_side') or 'none'}",
            f"Price: {fmt_price(status.get('last_trade_price'))}",
            f"Notional: {fmt_money(status.get('last_trade_notional'))}",
        ]
    )


def exchange_snapshot(cfg: BotConfig) -> str:
    state, orders, position = get_exchange_data(cfg)
    return "\n\n".join([format_account(state), format_position(position, cfg.default_symbol), format_orders(orders, cfg.default_symbol)])


def stop_service() -> str:
    rc, out = run_cmd(["systemctl", "stop", SERVICE_NAME], timeout=30)
    active, enabled = service_state()
    return f"⏸ STOP\nExecution: LIVE\nResult: rc={rc}\nActive: {active}\nEnabled: {enabled}\n{out}".strip()


def start_service() -> str:
    rc, out = run_cmd(["systemctl", "start", SERVICE_NAME], timeout=30)
    time.sleep(2)
    active, enabled = service_state()
    return f"▶️ START\nExecution: LIVE\nResult: rc={rc}\nActive: {active}\nEnabled: {enabled}\n{out}".strip()


def restart_service() -> str:
    rc, out = run_cmd(["systemctl", "restart", SERVICE_NAME], timeout=30)
    time.sleep(2)
    active, enabled = service_state()
    return f"🔄 RESTART\nExecution: LIVE\nResult: rc={rc}\nActive: {active}\nEnabled: {enabled}\n{out}".strip()


def cancel_all_orders(cfg: BotConfig) -> str:
    client = make_client(cfg)
    before = len(client.get_open_orders(cfg.default_symbol))
    try:
        canceled = client.cancel_all_orders(cfg.default_symbol)
    except Exception as exc:
        return f"❌ CANCEL ERROR\nExecution: LIVE\n{type(exc).__name__}: {exc}\nOpen orders before: {before}"
    after_orders = client.get_open_orders(cfg.default_symbol)
    return "\n".join(["🧹 CANCEL ALL", "Execution: LIVE", f"Open before: {before}", f"Canceled: {canceled}", f"Open after: {len(after_orders)}"])


def close_position(cfg: BotConfig) -> str:
    client = make_client(cfg)
    position = client.get_position(cfg.default_symbol)
    if not position:
        return "🚪 CLOSE POSITION\nExecution: LIVE\n\n✅ Already flat."
    try:
        size = float(position.get("szi") or position.get("size") or 0.0)
    except Exception:
        size = 0.0
    if abs(size) < 1e-12:
        return "🚪 CLOSE POSITION\nExecution: LIVE\n\n✅ Already flat."

    try:
        client.require_live_execution_support()
        assert client.exchange is not None
        if hasattr(client.exchange, "market_close"):
            result = client.exchange.market_close(cfg.default_symbol)
        else:
            mid = client.get_mid_price(cfg.default_symbol)
            side = "buy" if size < 0 else "sell"
            result = client.place_limit_order(cfg.default_symbol, side, abs(size), mid, reduce_only=True)
    except Exception as exc:
        return f"❌ CLOSE POSITION ERROR\nExecution: LIVE\n{type(exc).__name__}: {exc}"

    after = client.get_position(cfg.default_symbol)
    return "\n".join(
        [
            "🚪 CLOSE POSITION",
            "Execution: LIVE",
            f"Symbol: {cfg.default_symbol}",
            f"Requested size: {fmt_num(size, 8)} BTC",
            f"Result: {json.dumps(result, ensure_ascii=False)[:1200]}",
            "",
            format_position(after, cfg.default_symbol),
        ]
    )


def panic(cfg: BotConfig) -> str:
    parts = ["🚨 PANIC / SAFE MODE\nExecution: LIVE", stop_service(), cancel_all_orders(cfg), exchange_snapshot(cfg)]
    return "\n\n".join(parts)


def help_text() -> str:
    return """🤖 HYPERLIQUID LIVE BOT VEZÉRLÉS
Execution: LIVE

🧭 /menu - inline gombos vezérlő menü
📊 /status - szép bot + exchange státusz
📋 /orders - nyitott orderek designos nézetben
📌 /position - aktuális pozíció
📈 /trades - utolsó trade-ek logs/trades.jsonl alapján
📉 /performance - statisztika logs/trades.jsonl alapján
▶️ /startbot - live service indítása
⏸ /stopbot - live service leállítása
🔄 /restartbot - live service újraindítása
⬇️ /pull vagy /update - fix git pull + LIVE bot újraindító script futtatása
🧹 /cancelall - stop + összes open order törlése
🚪 /closeposition - aktuális pozíció zárása
🚨 /panic - vészstop: stop + cancel + snapshot
❓ /help - parancslista

Biztonság:
- Csak whitelistelt Telegram chat ID használhatja.
- /cancelall és /panic először leállítja a service-t, hogy ne rakja vissza az ordereket.
- A veszélyes gombos műveletek külön megerősítést kérnek.
"""


def handle_command(cfg: BotConfig, text: str) -> str:
    command = text.strip().split()[0].split("@")[0].lower()

    if command in {"/help", "/start"}:
        return help_text()
    if command == "/menu":
        return menu_text()
    if command == "/status":
        return compact_status() + "\n\n" + exchange_snapshot(cfg)
    if command == "/startbot":
        return start_service()
    if command == "/stopbot":
        return stop_service()
    if command == "/restartbot":
        return restart_service()
    if command in {"/pull", "/update"}:
        return "Use the Telegram poll loop for /pull or /update so progress messages can be sent safely."
    if command == "/cancelall":
        return stop_service() + "\n\n" + cancel_all_orders(cfg)
    if command == "/panic":
        return panic(cfg)
    if command == "/orders":
        _, orders, _ = get_exchange_data(cfg)
        return format_orders(orders, cfg.default_symbol)
    if command == "/position":
        state, _, pos = get_exchange_data(cfg)
        return format_position(pos, cfg.default_symbol) + "\n\n" + format_account(state)
    if command == "/trades":
        return format_trades()
    if command == "/performance":
        return format_performance()
    if command == "/closeposition":
        return close_position(cfg)
    return "Ismeretlen parancs. /help\nExecution: LIVE"


def handle_callback(cfg: BotConfig, data: str) -> tuple[str, dict[str, Any] | None]:
    if data == "cmd:menu":
        return menu_text(), main_menu_keyboard()
    if data.startswith("cmd:"):
        action = data.split(":", 1)[1]
        return handle_command(cfg, f"/{action}"), main_menu_keyboard()
    if data.startswith("confirm:"):
        action = data.split(":", 1)[1]
        labels = {
            "stopbot": "🛑 Stop bot",
            "cancelall": "❌ Cancel all open orders",
            "closeposition": "🚪 Close current position",
        }
        return (
            "\n".join(
                [
                    "⚠️ CONFIRM ACTION",
                    "Execution: LIVE",
                    "",
                    labels.get(action, action),
                    "",
                    "This action affects the live bot/account.",
                ]
            ),
            confirm_keyboard(action),
        )
    if data.startswith("do:"):
        action = data.split(":", 1)[1]
        return handle_command(cfg, f"/{action}"), main_menu_keyboard()
    return "Unknown button. Use /menu.\nExecution: LIVE", main_menu_keyboard()


def read_offset() -> int | None:
    try:
        if OFFSET_FILE.exists():
            return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return None


def write_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_FILE.write_text(str(offset), encoding="utf-8")


def poll_loop() -> None:
    cfg = load_config()
    if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    allowed = allowed_chat_ids(cfg)
    offset = read_offset()

    try:
        register_bot_commands(cfg.telegram_bot_token)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
        pass
    send_message(
        cfg.telegram_bot_token,
        cfg.telegram_chat_id,
        "✅ Telegram live control started.\nExecution: LIVE\n\nUse /menu for buttons.",
        main_menu_keyboard(),
    )

    while True:
        try:
            payload: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
            if offset is not None:
                payload["offset"] = offset
            resp = requests.get(telegram_api(cfg.telegram_bot_token, "getUpdates"), params=payload, timeout=POLL_TIMEOUT_SECONDS + 10)
            resp.raise_for_status()
            data = resp.json()
            for update in data.get("result", []):
                update_id = int(update.get("update_id", 0))
                offset = update_id + 1
                write_offset(offset)

                callback = update.get("callback_query") or {}
                if callback:
                    callback_id = str(callback.get("id", ""))
                    message = callback.get("message") or {}
                    chat = message.get("chat") or {}
                    chat_id = str(chat.get("id", ""))
                    data_value = str(callback.get("data", "") or "")

                    if chat_id not in allowed:
                        answer_callback(cfg.telegram_bot_token, callback_id, "Unauthorized")
                        if cfg.telegram_chat_id:
                            send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, f"⚠️ Unauthorized Telegram button attempt chat_id={chat_id}")
                        continue

                    answer_callback(cfg.telegram_bot_token, callback_id)
                    try:
                        cfg = load_config()
                        reply, markup = handle_callback(cfg, data_value)
                    except Exception as exc:
                        reply, markup = f"❌ Button error\nExecution: LIVE\n{type(exc).__name__}: {exc}", main_menu_keyboard()
                    send_message(cfg.telegram_bot_token, chat_id, reply, markup)
                    continue

                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = str(message.get("text", "") or "").strip()
                if not text.startswith("/"):
                    continue

                command = text.strip().split()[0].split("@")[0].lower()

                if command in {"/pull", "/update"}:
                    cfg = load_config()
                    if chat_id != cfg.telegram_chat_id:
                        logger.warning("Unauthorized Telegram /pull attempt chat_id=%s", chat_id)
                        if cfg.telegram_chat_id:
                            send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, f"⚠️ Jogosulatlan Telegram /pull próbálkozás chat_id={chat_id}")
                        continue

                    send_message(
                        cfg.telegram_bot_token,
                        chat_id,
                        f"⏳ Git pull + LIVE bot újraindítás indul. Timeout: {PULL_RESTART_TIMEOUT_SECONDS}s.\nExecution: LIVE",
                    )
                    _, reply = run_pull_restart_script()
                    send_message(cfg.telegram_bot_token, chat_id, reply)
                    continue

                if chat_id not in allowed:
                    if cfg.telegram_chat_id:
                        send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, f"⚠️ Jogosulatlan Telegram command próbálkozás chat_id={chat_id}: {text[:80]}")
                    continue

                try:
                    cfg = load_config()
                    reply = handle_command(cfg, text)
                except Exception as exc:
                    reply = f"❌ Command error\nExecution: LIVE\n{type(exc).__name__}: {exc}"
                markup = main_menu_keyboard() if command in {"/start", "/help", "/menu"} else None
                send_message(cfg.telegram_bot_token, chat_id, reply, markup)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            time.sleep(POLL_SLEEP_SECONDS)
            continue
        except Exception as exc:
            try:
                send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, f"⚠️ Telegram control loop error\nExecution: LIVE\n{type(exc).__name__}: {exc}")
            except Exception:
                pass
            time.sleep(5)
        time.sleep(POLL_SLEEP_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    poll_loop()
