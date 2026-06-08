from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


class TelegramHandler:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.last_error: dict = {}
        self.last_update_id: int | None = None

    def send(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            self.last_error = {"exception_type": "MissingCredentials", "http_status": None}
            return False
        payload = text[:3900]
        try:
            resp = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": payload}, timeout=10)
            self.last_error = {"exception_type": "", "http_status": resp.status_code}
            return resp.ok
        except Exception as exc:
            self.last_error = {"exception_type": type(exc).__name__, "http_status": getattr(getattr(exc, "response", None), "status_code", None)}
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
        regime = report.get("regime", "unknown")
        mode = report.get("mode", report.get("grid_mode", "unknown"))
        blocked_1h = report.get("blocked_risk_decisions_1h", report.get("blocked_risk_decisions_30m", 0))
        last_real_trade_age_hours = report.get("last_real_trade_age_hours")
        no_fill_cycles = report.get("no_fill_cycles", 0)
        top_block_reason = report.get("last_block_reason") or "none"
        trades_1h = report.get("trades_1h", report.get("trades_30m", 0))
        allowed_to_trade = bool(report.get("allowed_to_trade", False))
        pause_reason = report.get("pause_reason") or "none"
        strategy_status = report.get("strategy_status", "unknown")
        risk_state = report.get("risk_state", "unknown")
        next_order_side = (report.get("next_order_side") or "n/a").upper()
        next_order_price = money(report.get("next_order_price"))
        allowed_to_reduce = bool(report.get("allowed_to_reduce", False))
        next_exit_side = (report.get("next_exit_side") or "n/a").upper()
        next_exit_price = money(report.get("next_exit_price"))
        dust_one_sided_warning = bool(report.get("one_sided_grid_due_to_dust", False))

        human = "A bot aktív, de várakozik, mert az ár nem érte el a grid szinteket."
        if pause_reason in {"neutral_blocked_in_trend", "neutral_entries_blocked_in_trend"}:
            human = "Trend piacban a neutral grid tiltva van, ezért nincs új order."
        elif not allowed_to_trade:
            human = "Risk/strategy block miatt nincs új pozícióépítés."
        elif trades_1h == 0 and allowed_to_trade:
            human = "Nincs fill, mert az ár nem érte el a következő grid szintet."
        elif report.get("position_side") in {"LONG", "SHORT"}:
            human = f"A bot aktív és pozícióban van, jelenleg {str(report.get('position_side')).lower()} exposure-t tart."
        no_recent_trade_line = "Nincs új BUY/SELL fill az elmúlt 1 órában." if trades_1h == 0 else None
        lines = [
            "📊 Hyperliquid bot órás státusz",
            "",
            "Állapot:",
            f"Bot aktív, {regime} / {mode} módban vár.",
            f"strategy_status: {strategy_status}",
            f"risk_state: {risk_state}",
            f"pause_reason: {pause_reason}",
            f"last_block_reason: {top_block_reason}",
            f"allowed_to_trade: {allowed_to_trade}",
            f"allowed_to_reduce: {allowed_to_reduce}",
            f"next_order: {next_order_side} @ {next_order_price}",
            f"next_exit_order: {next_exit_side} @ {next_exit_price}",
            f"Új belépés engedélyezve: {'igen' if allowed_to_trade else 'nem'}",
            f"Pozíciózárás engedélyezve: {'igen' if allowed_to_reduce else 'nem'}",
            f"Következő exit order: {next_exit_side} @ {next_exit_price}",
            f"Miért nincs új grid order: {pause_reason if pause_reason != 'none' else 'n/a'}",
            f"Mit vár a bot a folytatáshoz: {report.get('resume_condition') or 'trend/momentum változás vagy kockázati blokk feloldása'}",
            f"Nearest BUY distance: {pct(report.get('distance_to_buy_pct'))}",
            f"Nearest SELL distance: {pct(report.get('distance_to_sell_pct'))}",
            "",
            "Market stress:",
            f"Enabled: {report.get('market_stress_enabled')}",
            f"Volatility: {report.get('volatility_mode', 'n/a')}",
            f"Stress score: {num(report.get('market_stress_score'), 3)}",
            f"Trend strength: {num(report.get('trend_strength_score'), 3)}",
            f"Transition: {num(report.get('trend_transition_score'), 3)} / {report.get('transition_direction', 'NONE')} / conf {num(report.get('transition_confidence'), 3)}",
            f"Opportunity: {report.get('opportunity_mode', 'n/a')}",
            f"BTC 5m / 1h: {pct(report.get('btc_5m_return_pct'))} / {pct(report.get('btc_1h_return_pct'))}",
            f"Reason: {report.get('market_stress_reason') or 'none'}",
            "",
            "Account:",
            f"Equity: {money(report.get('equity'))}",
            f"Cash: {money(report.get('cash'))}",
            f"Realized PnL: {money(report.get('realized_pnl'))}",
            f"Unrealized PnL: {money(report.get('unrealized_pnl'))}",
            f"Fees paid: {money(report.get('fees_paid'))}",
            f"Total PnL: {money(report.get('total_pnl'))} ({pct(report.get('total_pnl_pct'))})",
            f"Daily start equity: {money(report.get('daily_start_equity'))}",
            f"Daily realized PnL: {money(report.get('daily_realized_pnl'))}",
            f"Daily unrealized PnL: {money(report.get('daily_unrealized_pnl'))}",
            f"Daily fees: {money(report.get('daily_fees'))}",
            f"Net daily PnL: {money(report.get('net_daily_pnl', report.get('daily_pnl')))} ({pct(report.get('daily_pnl_pct'))})",
            "",
            "Pozíció:",
            f"Symbol: {report.get('symbol', 'n/a')}",
            f"Side: {report.get('position_side', 'n/a')}",
            f"Size: {num(report.get('position_size'), 8)} {report.get('symbol', '')}",
            f"Notional: {money(report.get('position_notional'))}",
            f"Mark price: {money(report.get('mark_price', report.get('price')))}",
            f"Avg entry: {money(report.get('avg_entry'))}",
            f"Unrealized PnL: {money(report.get('unrealized_pnl'))} ({pct(report.get('unrealized_pnl_pct'))})",
            f"Exposure: {pct(report.get('exposure_pct'))}",
            "",
            "Grid:",
            f"Nearest BUY: {money(report.get('nearest_buy_price'))} | Distance: {pct(report.get('distance_to_buy_pct'))}",
            f"Nearest SELL: {money(report.get('nearest_sell_price'))} | Distance: {pct(report.get('distance_to_sell_pct'))}",
            f"Active orders: BUY {report.get('active_buy_orders', 0)} / SELL {report.get('active_sell_orders', 0)}",
            f"Orphan cleanup count: {report.get('orphan_order_cleanup_count', 0)}",
            f"Stale cleanup count: {report.get('stale_order_cleanup_count', 0)}",
            ("⚠️ One-sided grid aktív dust pozíció miatt." if dust_one_sided_warning else None),
            f"Grid spacing: {pct(report.get('grid_spacing_pct'))}, levels: {report.get('grid_levels', 'n/a')}",
            f"Expected net edge: {money(report.get('expected_net_edge'))} ({pct(report.get('expected_net_edge_pct'))}), RR: {num(report.get('expected_rr_ratio'), 2)}",
            f"Edge filter skipped: {report.get('edge_filter_skipped_orders', 0)}",
            "",
            "Trade analytics:",
            f"Profit Factor: {num(report.get('profit_factor'), 2)}",
            f"Expectancy: {money(report.get('expectancy'))}",
            f"Avg profit/trade: {money(report.get('avg_profit_per_trade'))}, Trades/day: {num(report.get('trades_per_day'), 1)}",
            f"Avg winner / loser: {money(report.get('avg_profit_per_winner', report.get('avg_winner')))} / {money(report.get('avg_loss_per_loser', report.get('avg_loser')))}",
            f"Avg winner before/after: {money(report.get('avg_winner_before'))} / {money(report.get('avg_winner_after'))}",
            f"Fee Ratio: {pct(report.get('fee_gross_profit_ratio'))}",
            f"Gross before fees: {money(report.get('gross_pnl_before_fees'))}, Total fees: {money(report.get('total_fees'))}, Net: {money(report.get('net_pnl'))}",
            f"Fee/gross profit ratio: {pct(report.get('fee_to_gross_profit_ratio'))}",
            f"Avg net/closed trade: {money(report.get('avg_net_profit_per_closed_trade'))}, Avg fee/trade: {money(report.get('avg_fee_per_trade'))}",
            f"Profit factor after fees: {num(report.get('profit_factor_after_fees'), 2)}",
            f"Min notional blocked: {report.get('min_notional_blocked_count', 0)}, Anti-chop triggers: {report.get('anti_chop_trigger_count', 0)}",
            "Maker/Taker breakdown:",
            f"Maker: {report.get('maker_trades', 0)} trades, fees {money(report.get('maker_fee_total'))}, ratio {num(report.get('maker_ratio_pct'), 1)}%",
            f"Taker: {report.get('taker_trades', 0)} trades, fees {money(report.get('taker_fee_total'))}, ratio {num(report.get('taker_ratio_pct'), 1)}%",
            f"Dust trades: {report.get('dust_trade_count', 0)}, volume {money(report.get('dust_volume'))}, excluded: {report.get('exclude_dust_from_performance_stats')}",
            "Regime Performance:",
            f"Trades: {report.get('trades_by_regime', {})}",
            f"Winrate: {report.get('winrate_by_regime', {})}",
            f"PnL: {report.get('pnl_by_regime', {})}",
            f"Profit factor: {report.get('profit_factor_by_regime', {})}",
            "Long vs Short:",
            f"LONG: Trades {report.get('long_trades', 0)}, PnL {money(report.get('pnl_long_trades'))}, Winrate {num(report.get('long_winrate_pct'), 1)}%",
            f"SHORT: Trades {report.get('short_trades', 0)}, PnL {money(report.get('pnl_short_trades'))}, Winrate {num(report.get('short_winrate_pct'), 1)}%",
            f"Avg holding time: {num((report.get('average_holding_time_seconds') or 0) / 60, 1)} min",
            "",
            "Utolsó 1 óra:",
            f"BUY count: {report.get('buy_count_1h', report.get('buy_count_30m', 0))}",
            f"SELL count: {report.get('sell_count_1h', report.get('sell_count_30m', 0))}",
            no_recent_trade_line if no_recent_trade_line else f"Trades: {trades_1h}",
            f"Volume: {money(report.get('volume_usd_1h', report.get('volume_usd_30m')))}, Fees: {money(report.get('fees_1h', report.get('fees_30m')))}",
            f"Realized PnL delta: {money(report.get('realized_pnl_delta_1h', report.get('realized_pnl_delta_30m')))}",
            f"Blocked risk decisions: {blocked_1h}, reasons: {report.get('blocked_risk_decisions_by_reason_1h', report.get('blocked_risk_decisions_by_reason', {}))}",
            f"Top block reason: {top_block_reason}",
            f"No fill cycles: {no_fill_cycles}",
            "",
            "Utolsó trade:",
            f"{(report.get('last_trade_side') or 'none').upper()} @ {report.get('last_trade_price') if report.get('last_trade_price') is not None else 'n/a'}",
            f"Qty: {num(report.get('last_trade_qty'), 8)} {report.get('symbol', '')}, Notional: {money(report.get('last_trade_notional'))}",
            f"Fee: {money(report.get('last_trade_fee'))}, PnL delta: {money(report.get('last_trade_realized_pnl'))}",
            f"Reason: {report.get('last_trade_reason') or 'none'}",
            f"Last real trade ts: {report.get('last_real_trade_ts') or 'n/a'}",
            f"Last real trade age: {num(last_real_trade_age_hours, 2)} h",
            f"Next intended order: {(report.get('next_order_side') or 'n/a').upper()} @ {money(report.get('next_order_price'))}",
            ("⚠️ WARNING: no real trade for more than 2 hours." if isinstance(last_real_trade_age_hours, (int, float)) and last_real_trade_age_hours > 2 else None),
            "",
            "Döntés:",
            human,
            f"Updated: {ts}",
        ]
        return "\n".join(line for line in lines if line is not None)[:3900]

    def format_fill_alert(self, trade: dict, *, mode_name: str) -> str:
        side = str(trade.get("side", "")).upper()
        title = "🟢 BUY EXECUTED" if side == "BUY" else "🔴 SELL EXECUTED"
        symbol = trade.get("symbol", "n/a")
        price = float(trade.get("price", 0.0))
        qty = float(trade.get("qty", 0.0))
        notional = float(trade.get("notional", 0.0))
        fee = float(trade.get("fee", 0.0))
        realized_delta = float(trade.get("realized_pnl_delta", 0.0))
        realized_total = float(trade.get("realized_pnl_total", 0.0))
        position_size = float(trade.get("position_size", trade.get("position_after", 0.0)))
        position_notional = float(trade.get("position_notional", 0.0))
        equity = float(trade.get("equity", 0.0))
        position_side = "FLAT"
        if position_size > 0:
            position_side = "LONG"
        elif position_size < 0:
            position_side = "SHORT"
        return "\n".join([
            title,
            "",
            f"Symbol: {symbol}",
            f"Mode: {mode_name}",
            f"Price: {price:.2f} USD",
            f"Qty: {qty:.8f} {symbol}",
            f"Notional: {notional:.2f} USD",
            f"Fee: {fee:.4f} USD",
            f"Liquidity: {trade.get('fill_liquidity', 'maker')}",
            f"Dust fill: {trade.get('dust_fill', False)}",
            "",
            f"Position after: {position_side}",
            f"Position size: {position_size:.8f} {symbol}",
            f"Position notional: {position_notional:.2f} USD",
            "",
            f"Realized PnL delta: {realized_delta:+.4f} USD",
            f"Realized PnL total: {realized_total:+.4f} USD",
            f"Equity: {equity:.2f} USD",
            "",
            f"Regime: {trade.get('regime', 'unknown')}",
            f"Strategy mode: {trade.get('mode', 'unknown')}",
            f"Risk: {trade.get('risk_state', 'unknown')}",
            f"Pause: {trade.get('pause_reason') or 'none'}",
            f"Reason: {trade.get('reason') or 'none'}",
            f"Time: {str(trade.get('timestamp', '')).replace('T', ' ').replace('+00:00', ' UTC')}",
        ])[:3900]

    def send_status_report(self, report: dict) -> bool:
        return self.send(self.format_status_report(report))

    def fetch_commands(self) -> list[dict]:
        if not self.token:
            return []
        offset = self.last_update_id + 1 if self.last_update_id is not None else None
        params = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates", params=params, timeout=10)
            if not resp.ok:
                return []
            payload = resp.json()
            updates = payload.get("result", [])
            for upd in updates:
                uid = upd.get("update_id")
                if isinstance(uid, int):
                    self.last_update_id = uid
            return updates
        except Exception:
            return []
