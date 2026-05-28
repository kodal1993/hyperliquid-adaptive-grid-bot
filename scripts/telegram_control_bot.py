#!/usr/bin/env python3
from __future__ import annotations

import json
import os
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
OFFSET_FILE = Path(os.getenv("TELEGRAM_CONTROL_OFFSET_FILE", "state/telegram_control.offset"))
POLL_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_CONTROL_POLL_TIMEOUT_SECONDS", "25"))
POLL_SLEEP_SECONDS = float(os.getenv("TELEGRAM_CONTROL_POLL_SLEEP_SECONDS", "1"))
MAX_MESSAGE_LEN = 3800
BOT_COMMANDS = [
    {"command": "status", "description": "Bot + exchange státusz"},
    {"command": "startbot", "description": "Live bot service indítása"},
    {"command": "stopbot", "description": "Live bot service leállítása"},
    {"command": "restartbot", "description": "Live bot service újraindítása"},
    {"command": "cancelall", "description": "Stop + összes open order törlése"},
    {"command": "panic", "description": "Vészstop: stop + cancel + snapshot"},
    {"command": "orders", "description": "Open orderek lekérése"},
    {"command": "position", "description": "Aktuális pozíció lekérése"},
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


def send_message(token: str, chat_id: str, text: str) -> None:
    chunks = [text[i : i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)] or [text]
    for chunk in chunks:
        requests.post(telegram_api(token, "sendMessage"), json={"chat_id": chat_id, "text": chunk}, timeout=15).raise_for_status()


def run_cmd(args: list[str], *, timeout: int = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output.strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


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
    lines = ["📋 OPEN ORDERS", f"Symbol: {symbol}", f"Count: {len(orders)}", ""]
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
    lines = ["📌 POSITION", f"Symbol: {symbol}", ""]
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

    return "📌 POSITION\n" + json.dumps(position, indent=2, ensure_ascii=False)[:2500]


def format_account(state: dict[str, Any]) -> str:
    margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
    return "\n".join(
        [
            "🏦 ACCOUNT",
            f"Equity: {fmt_money(margin.get('accountValue'))}",
            f"Withdrawable: {fmt_money(state.get('withdrawable') if isinstance(state, dict) else None)}",
            f"Total notional pos: {fmt_money(margin.get('totalNtlPos'))}",
            f"Margin used: {fmt_money(margin.get('totalMarginUsed'))}",
        ]
    )


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
            f"Service: {'🟢' if active == 'active' else '🔴'} {active} | enabled: {enabled}",
            "",
            "⚙️ STRATÉGIA",
            f"Állapot: {status.get('status')} | paper: {status.get('paper_mode')}",
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
    return f"⏸ STOP\nResult: rc={rc}\nActive: {active}\nEnabled: {enabled}\n{out}".strip()


def start_service() -> str:
    rc, out = run_cmd(["systemctl", "start", SERVICE_NAME], timeout=30)
    time.sleep(2)
    active, enabled = service_state()
    return f"▶️ START\nResult: rc={rc}\nActive: {active}\nEnabled: {enabled}\n{out}".strip()


def restart_service() -> str:
    rc, out = run_cmd(["systemctl", "restart", SERVICE_NAME], timeout=30)
    time.sleep(2)
    active, enabled = service_state()
    return f"🔄 RESTART\nResult: rc={rc}\nActive: {active}\nEnabled: {enabled}\n{out}".strip()


def cancel_all_orders(cfg: BotConfig) -> str:
    client = make_client(cfg)
    before = len(client.get_open_orders(cfg.default_symbol))
    try:
        canceled = client.cancel_all_orders(cfg.default_symbol)
    except Exception as exc:
        return f"❌ CANCEL ERROR\n{type(exc).__name__}: {exc}\nOpen orders before: {before}"
    after_orders = client.get_open_orders(cfg.default_symbol)
    return "\n".join(["🧹 CANCEL ALL", f"Open before: {before}", f"Canceled: {canceled}", f"Open after: {len(after_orders)}"])


def panic(cfg: BotConfig) -> str:
    parts = ["🚨 PANIC / SAFE MODE", stop_service(), cancel_all_orders(cfg), exchange_snapshot(cfg)]
    return "\n\n".join(parts)


def help_text() -> str:
    return """🤖 HYPERLIQUID LIVE BOT VEZÉRLÉS

📊 /status - szép bot + exchange státusz
📋 /orders - nyitott orderek designos nézetben
📌 /position - aktuális pozíció
▶️ /startbot - live service indítása
⏸ /stopbot - live service leállítása
🔄 /restartbot - live service újraindítása
🧹 /cancelall - stop + összes open order törlése
🚨 /panic - vészstop: stop + cancel + snapshot
❓ /help - parancslista

Biztonság:
- Csak whitelistelt Telegram chat ID használhatja.
- /cancelall és /panic először leállítja a service-t, hogy ne rakja vissza az ordereket.
- Market flatten nincs automatikusan engedélyezve.
"""


def handle_command(cfg: BotConfig, text: str) -> str:
    command = text.strip().split()[0].split("@")[0].lower()

    if command in {"/help", "/start"}:
        return help_text()
    if command == "/status":
        return compact_status() + "\n\n" + exchange_snapshot(cfg)
    if command == "/startbot":
        return start_service()
    if command == "/stopbot":
        return stop_service()
    if command == "/restartbot":
        return restart_service()
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
    return "Ismeretlen parancs. /help"


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

    register_bot_commands(cfg.telegram_bot_token)
    send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, "✅ Telegram live control elindult. Designos parancsok aktívak. /help")

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

                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = str(message.get("text", "") or "").strip()
                if not text.startswith("/"):
                    continue

                if chat_id not in allowed:
                    if cfg.telegram_chat_id:
                        send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, f"⚠️ Jogosulatlan Telegram command próbálkozás chat_id={chat_id}: {text[:80]}")
                    continue

                try:
                    cfg = load_config()
                    reply = handle_command(cfg, text)
                except Exception as exc:
                    reply = f"❌ Command error: {type(exc).__name__}: {exc}"
                send_message(cfg.telegram_bot_token, chat_id, reply)
        except Exception as exc:
            try:
                send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, f"⚠️ Telegram control loop error: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            time.sleep(5)
        time.sleep(POLL_SLEEP_SECONDS)


if __name__ == "__main__":
    poll_loop()
