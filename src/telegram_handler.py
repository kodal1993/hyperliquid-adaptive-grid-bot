from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        payload = text[:3900]
        try:
            resp = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": payload}, timeout=10)
            return resp.ok
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)
            return False

    def format_status_report(self, report: dict) -> str:
        def money(v: float | None) -> str:
            return "n/a" if v is None else f"${v:,.2f}"

        def pct(v: float | None) -> str:
            return "n/a" if v is None else f"{v * 100:.2f}%"

        def num(v: float | None, d: int = 6) -> str:
            return "n/a" if v is None else f"{v:.{d}f}"

        ts = report.get("updated") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines = [
            "🤖 Hyperliquid Grid Bot",
            "━━━━━━━━━━━━━━━━━━",
            f"🟢 Status: {report.get('status', 'n/a').upper()}",
            f"🌐 Network: {report.get('network', 'n/a')}",
            f"🧪 Mode: {report.get('mode_name', 'n/a')}",
            f"🪙 Symbol: {report.get('symbol', 'n/a')}",
            f"🧠 Regime: {report.get('regime', 'n/a')}",
            f"🎯 Grid Mode: {report.get('mode', report.get('grid_mode', 'n/a'))}",
            f"🎛 Fill Model: {report.get('fill_model', 'n/a')}",
            f"✅ Allowed To Trade: {report.get('allowed_to_trade', 'n/a')}",
            "",
            "💰 Account",
            f"Equity: {money(report.get('equity'))}",
            f"Cash: {money(report.get('cash'))}",
            f"Realized PnL: {money(report.get('realized_pnl'))}",
            f"Unrealized PnL: {money(report.get('unrealized_pnl'))}",
            f"Fees Paid: {money(report.get('fees_paid'))}",
            f"Daily PnL: {pct(report.get('daily_pnl_pct'))}",
            "",
            "📦 Position",
            f"Side: {report.get('position_side', 'n/a')}",
            f"Size (coin): {num(report.get('position_size'))} {report.get('symbol', '')}",
            f"Notional: {money(report.get('position_notional'))}",
            f"Avg Entry: {money(report.get('avg_entry'))}",
            f"Mark Price: {money(report.get('mark_price', report.get('price')))}",
            "",
            "📊 Orders / Grid",
            f"Open Paper Orders: {report.get('open_orders', 'n/a')}",
            f"Last Regrid: {report.get('last_regrid', 'n/a')}",
            f"Order Size: {num(report.get('order_size'))} {report.get('symbol', '')}",
            f"Order Notional: {money(report.get('order_notional'))}",
            f"Grid Levels: {report.get('grid_levels', 'n/a')}",
            f"Grid Spacing: {pct(report.get('grid_spacing_pct'))}",
            "",
            "🛡 Risk",
            f"Max Position: {money(report.get('max_position'))}",
            f"Drawdown: {pct(report.get('drawdown'))}",
            f"Daily Loss Limit: {pct(report.get('daily_loss_limit_pct'))}",
            f"Risk State: {report.get('risk_state', 'n/a')}",
            f"Pause Reason: {report.get('pause_reason') or report.get('risk_reason', 'none')}",
            f"Blocked Decisions (30m): {report.get('blocked_risk_decisions_30m', 0)}",
            f"Blocked By Reason: {report.get('blocked_risk_decisions_by_reason', {})}",
            f"Last Block Reason: {report.get('last_block_reason') or 'none'}",
            "",
            "🧾 Last Trade",
            f"Side: {report.get('last_trade_side') or 'none'}",
            f"Price: {money(report.get('last_trade_price'))}",
            f"Qty (coin): {num(report.get('last_trade_qty'), 8)}",
            f"Notional: {money(report.get('last_trade_notional'))}",
            f"Fee: {money(report.get('last_trade_fee'))}",
            f"Realized PnL: {money(report.get('last_trade_realized_pnl'))}",
            f"Reason: {report.get('last_trade_reason') or 'none'}",
            "",
            "⚡ Last Tick",
            f"Fills This Tick: {report.get('fills_count', 0)}",
            f"Updated: {ts}",
        ]
        return "\n".join(lines)[:3900]

    def send_status_report(self, report: dict) -> bool:
        return self.send(self.format_status_report(report))

    def command_handlers(self) -> list[str]:
        return ["/status", "/pause", "/resume", "/risk", "/positions", "/orders"]
