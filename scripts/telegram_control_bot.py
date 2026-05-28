#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from src.config import BotConfig
from src.hyperliquid_client import HyperliquidClient

SERVICE_NAME = os.getenv("TELEGRAM_CONTROL_SERVICE_NAME", "hyperliquid-grid-bot-live")
STATUS_FILE = Path(os.getenv("TELEGRAM_CONTROL_STATUS_FILE", "data/status.json"))
OFFSET_FILE = Path(os.getenv("TELEGRAM_CONTROL_OFFSET_FILE", "state/telegram_control.offset"))
POLL_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_CONTROL_POLL_TIMEOUT_SECONDS", "25"))
POLL_SLEEP_SECONDS = float(os.getenv("TELEGRAM_CONTROL_POLL_SLEEP_SECONDS", "1"))
MAX_MESSAGE_LEN = 3800


def load_config() -> BotConfig:
    return BotConfig.from_env()


def allowed_chat_ids(cfg: BotConfig) -> set[str]:
    raw = os.getenv("TELEGRAM_CONTROL_ALLOWED_CHAT_IDS", "") or cfg.telegram_chat_id
    return {item.strip() for item in raw.split(",") if item.strip()}


def telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def send_message(token: str, chat_id: str, text: str) -> None:
    chunks = [text[i : i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)] or [text]
    for chunk in chunks:
        requests.post(
            telegram_api(token, "sendMessage"),
            json={"chat_id": chat_id, "text": chunk},
            timeout=15,
        ).raise_for_status()


def run_cmd(args: list[str], *, timeout: int = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        output = (completed.stdout or "") + (completed.stderr or "")
        return completed.returncode, output.strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def service_state() -> str:
    active_rc, active = run_cmd(["systemctl", "is-active", SERVICE_NAME], timeout=10)
    enabled_rc, enabled = run_cmd(["systemctl", "is-enabled", SERVICE_NAME], timeout=10)
    return f"service={SERVICE_NAME}\nactive={active if active_rc == 0 else active or 'unknown'}\nenabled={enabled if enabled_rc == 0 else enabled or 'unknown'}"


def read_status() -> dict[str, Any]:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def compact_status() -> str:
    status = read_status()
    if not status:
        return f"⚠️ Nincs status fájl: {STATUS_FILE}"

    keys = [
        "status",
        "paper_mode",
        "symbol",
        "price",
        "regime",
        "mode",
        "risk_state",
        "pause_reason",
        "equity",
        "daily_pnl",
        "daily_pnl_pct",
        "position_side",
        "position_size",
        "position_notional",
        "unrealized_pnl",
        "realized_pnl",
        "open_orders",
        "active_buy_orders",
        "active_sell_orders",
        "nearest_buy_price",
        "nearest_sell_price",
        "last_trade_side",
        "last_trade_price",
        "last_trade_notional",
        "last_error",
    ]
    lines = ["📊 Hyperliquid live bot státusz", service_state(), ""]
    for key in keys:
        if key in status:
            lines.append(f"{key}: {status.get(key)}")
    return "\n".join(lines)


def make_client(cfg: BotConfig) -> HyperliquidClient:
    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()
    return client


def exchange_snapshot(cfg: BotConfig) -> str:
    client = make_client(cfg)
    state = client.get_user_state()
    margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
    orders = client.get_open_orders(cfg.default_symbol)
    position = client.get_position(cfg.default_symbol)
    return "\n".join(
        [
            "🏦 Exchange snapshot",
            f"accountValue: {margin.get('accountValue')}",
            f"withdrawable: {state.get('withdrawable') if isinstance(state, dict) else None}",
            f"position: {json.dumps(position, ensure_ascii=False)}",
            f"open_orders_count: {len(orders)}",
            f"open_orders: {json.dumps(orders, ensure_ascii=False)[:2500]}",
        ]
    )


def stop_service() -> str:
    rc, out = run_cmd(["systemctl", "stop", SERVICE_NAME], timeout=30)
    state = service_state()
    return f"systemctl stop rc={rc}\n{out}\n{state}".strip()


def start_service() -> str:
    rc, out = run_cmd(["systemctl", "start", SERVICE_NAME], timeout=30)
    time.sleep(2)
    state = service_state()
    return f"systemctl start rc={rc}\n{out}\n{state}".strip()


def restart_service() -> str:
    rc, out = run_cmd(["systemctl", "restart", SERVICE_NAME], timeout=30)
    time.sleep(2)
    state = service_state()
    return f"systemctl restart rc={rc}\n{out}\n{state}".strip()


def cancel_all_orders(cfg: BotConfig) -> str:
    client = make_client(cfg)
    before = len(client.get_open_orders(cfg.default_symbol))
    try:
        canceled = client.cancel_all_orders(cfg.default_symbol)
    except Exception as exc:
        return f"cancel_all_error: {type(exc).__name__}: {exc}\nopen_orders_before: {before}"
    after = len(client.get_open_orders(cfg.default_symbol))
    return f"open_orders_before: {before}\ncanceled_orders: {canceled}\nopen_orders_after: {after}"


def panic(cfg: BotConfig) -> str:
    parts = ["🚨 PANIC / SAFE MODE"]
    parts.append(stop_service())
    parts.append(cancel_all_orders(cfg))
    parts.append(exchange_snapshot(cfg))
    return "\n\n".join(parts)


def help_text() -> str:
    return """🤖 Hyperliquid live bot vezérlés

Parancsok:
/status - bot + exchange státusz
/startbot - live service indítása
/stopbot - live service leállítása
/restartbot - live service újraindítása
/cancelall - service stop + összes open order törlése
/panic - service stop + order cancel + exchange snapshot
/orders - exchange open orderek
/position - exchange pozíció
/help - parancslista

Biztonság:
- Csak whitelistelt Telegram chat ID használhatja.
- /cancelall és /panic először leállítja a service-t, hogy ne rakja vissza az ordereket.
- Pozíciót automatikusan nem zár markettel; először kézzel ellenőrizzük a /position alapján.
"""


def handle_command(cfg: BotConfig, text: str) -> str:
    command = text.strip().split()[0].split("@")[0].lower()

    if command in {"/help", "/start"}:
        return help_text()
    if command == "/status":
        return compact_status() + "\n\n" + exchange_snapshot(cfg)
    if command == "/startbot":
        return "▶️ Start live bot\n" + start_service()
    if command == "/stopbot":
        return "⏸ Stop live bot\n" + stop_service()
    if command == "/restartbot":
        return "🔄 Restart live bot\n" + restart_service()
    if command == "/cancelall":
        return "🧹 Cancel all: service stop + order cancel\n" + stop_service() + "\n\n" + cancel_all_orders(cfg)
    if command == "/panic":
        return panic(cfg)
    if command == "/orders":
        client = make_client(cfg)
        orders = client.get_open_orders(cfg.default_symbol)
        return f"📋 Open orders count: {len(orders)}\n{json.dumps(orders, indent=2, ensure_ascii=False)[:3500]}"
    if command == "/position":
        client = make_client(cfg)
        pos = client.get_position(cfg.default_symbol)
        return f"📌 Position {cfg.default_symbol}\n{json.dumps(pos, indent=2, ensure_ascii=False)}"
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

    send_message(cfg.telegram_bot_token, cfg.telegram_chat_id, "✅ Telegram live control elindult. /help")

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
                    # Reload config on every command so live.env edits take effect without restarting this controller.
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
