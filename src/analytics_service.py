from __future__ import annotations

import csv
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _parse_iso_utc(ts_raw: str) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class AnalyticsService:
    def __init__(self, trades_csv: Path, risk_decisions_csv: Path, symbol: str):
        self.trades_csv = trades_csv
        self.risk_decisions_csv = risk_decisions_csv
        self.symbol = symbol
        self._trades_file_offset: int = 0
        self._trades_header: list[str] | None = None
        self._trades_cache: list[dict] = []
        self._risk_file_offset: int = 0
        self._risk_header: list[str] | None = None
        self._risk_cache: list[dict] = []

    def _refresh_csv_cache(self, csv_path: Path, cache: list[dict], offset_attr: str, header_attr: str, *, valid_trades: bool = False) -> None:
        if not csv_path.exists():
            setattr(self, offset_attr, 0)
            setattr(self, header_attr, None)
            cache.clear()
            return
        current_size = csv_path.stat().st_size
        offset = int(getattr(self, offset_attr))
        if current_size < offset:
            offset = 0
            cache.clear()
            setattr(self, header_attr, None)
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            if offset <= 0:
                reader = csv.DictReader(f)
                setattr(self, header_attr, list(reader.fieldnames or []))
                for raw_row in reader:
                    row = self._sanitize_trade_csv_row(raw_row) if valid_trades else {k: v for k, v in raw_row.items() if k is not None}
                    if not valid_trades or self._is_valid_trade_row(row):
                        cache.append(row)
            else:
                header = getattr(self, header_attr)
                if not header:
                    f.seek(0)
                    header = next(csv.reader(f), [])
                    setattr(self, header_attr, list(header))
                    offset = f.tell()
                f.seek(offset)
                reader = csv.DictReader(f, fieldnames=header)
                for raw_row in reader:
                    row = self._sanitize_trade_csv_row(raw_row) if valid_trades else {k: v for k, v in raw_row.items() if k is not None}
                    if not any((v or "").strip() for v in row.values()):
                        continue
                    if not valid_trades or self._is_valid_trade_row(row):
                        cache.append(row)
            setattr(self, offset_attr, f.tell())

    def _refresh_trades_cache(self) -> None:
        self._refresh_csv_cache(self.trades_csv, self._trades_cache, "_trades_file_offset", "_trades_header", valid_trades=True)

    def _refresh_risk_cache(self) -> None:
        self._refresh_csv_cache(self.risk_decisions_csv, self._risk_cache, "_risk_file_offset", "_risk_header", valid_trades=False)

    def _is_valid_trade_row(self, row: dict) -> bool:
        symbol = str(row.get("symbol", "")).upper().strip()
        if not symbol or symbol != self.symbol.upper():
            return False
        price = _safe_float(row.get("price"))
        qty = _safe_float(row.get("qty"))
        if price is None or qty is None or qty <= 0:
            return False
        if symbol == "BTC" and price < 1000:
            return False
        return True


    def _read_last_valid_trade(self, csv_path: Path | None = None) -> dict:
        self._refresh_trades_cache()
        csv_path = self.trades_csv
        if not csv_path.exists():
            return {}
        last: dict = {}
        for row in self._trades_cache:
                if self._is_valid_trade_row(row):
                    last = row
        return last


    def _sanitize_trade_csv_row(self, row: dict) -> dict:
        sanitized = {key: value for key, value in row.items() if key is not None}
        if len(sanitized) != len(row):
            logger.warning("malformed_trade_csv_row_skipped_or_sanitized")
        return sanitized


    def write_last_100_real_trades(self, out_path: Path) -> None:
        self._refresh_trades_cache()
        csv_path = self.trades_csv
        if not csv_path.exists():
            return
        rows = list(self._trades_cache)
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not fields:
            fields = ["timestamp", "symbol", "side", "price", "qty", "notional", "fee", "realized_pnl_delta", "realized_pnl_total", "cash", "equity", "position_size", "position_notional", "regime", "mode", "risk_state", "pause_reason", "reason"]
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows[-100:])


    def risk_block_stats_window(self, now_ts: float, window_seconds: int = 3600) -> tuple[int, dict[str, int], str]:
        self._refresh_risk_cache()
        if not self.risk_decisions_csv.exists():
            return 0, {}, ""
        window_start = now_ts - window_seconds
        reason_counts: Counter[str] = Counter()
        last_block_reason = ""
        blocked_count = 0
        for row in self._risk_cache:
                if row.get("decision") != "block":
                    continue
                ts_raw = row.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                ts = dt.timestamp()
                if ts < window_start:
                    continue
                blocked_count += 1
                reason = row.get("block_reason") or "unknown"
                reason_counts[reason] += 1
                if ts >= window_start:
                    last_block_reason = reason
        return blocked_count, dict(reason_counts), last_block_reason


    def trade_stats_window(self, now_ts: float, window_seconds: int = 3600) -> dict:
        stats = {"trades_count": 0, "buy_count": 0, "sell_count": 0, "volume_usd": 0.0, "fees": 0.0, "realized_pnl_delta": 0.0, "last_trade_ts": "", "candidate_fills_count": 0, "no_orders_touched_count": 0, "maker_trades": 0, "taker_trades": 0, "maker_fee_total": 0.0, "taker_fee_total": 0.0, "dust_trade_count": 0, "dust_volume": 0.0}
        self._refresh_trades_cache()
        if self.trades_csv.exists():
            window_start = now_ts - window_seconds
            for row in self._trades_cache:
                    if not self._is_valid_trade_row(row):
                        continue
                    try:
                        dt = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if dt.timestamp() < window_start:
                        continue
                    stats["trades_count"] += 1
                    side = str(row.get("side", "")).lower()
                    if side == "buy":
                        stats["buy_count"] += 1
                    elif side == "sell":
                        stats["sell_count"] += 1
                    notional = _safe_float(row.get("notional")) or 0.0
                    fee = _safe_float(row.get("fee")) or 0.0
                    stats["volume_usd"] += notional
                    stats["fees"] += fee
                    stats["realized_pnl_delta"] += _safe_float(row.get("realized_pnl_delta")) or 0.0
                    liquidity = str(row.get("fill_liquidity") or "maker").lower()
                    if liquidity == "taker":
                        stats["taker_trades"] += 1
                        stats["taker_fee_total"] += fee
                    else:
                        stats["maker_trades"] += 1
                        stats["maker_fee_total"] += fee
                    if _is_truthy(row.get("dust_fill")) or (0.0 < notional < 1.0):
                        stats["dust_trade_count"] += 1
                        stats["dust_volume"] += notional
                    stats["last_trade_ts"] = row.get("timestamp", "")
        return stats



    def _empty_regime_stats(self) -> dict:
        return {"trades": 0, "wins": 0, "losses": 0, "winrate_pct": 0.0, "pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0}


    def _update_group_stats(self, groups: dict[str, dict], key: str, pnl: float) -> None:
        stats = groups.setdefault(key, self._empty_regime_stats())
        stats["trades"] += 1
        stats["pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1
            stats["gross_profit"] += pnl
        elif pnl < 0:
            stats["losses"] += 1
            stats["gross_loss"] += abs(pnl)


    def _finalize_group_stats(self, groups: dict[str, dict]) -> dict[str, dict]:
        for stats in groups.values():
            stats["winrate_pct"] = (stats["wins"] / stats["trades"] * 100.0) if stats["trades"] else 0.0
            stats["profit_factor"] = stats["gross_profit"] / stats["gross_loss"] if stats["gross_loss"] > 1e-12 else (stats["gross_profit"] if stats["gross_profit"] > 0 else 0.0)
        return groups


    def _volatility_bucket_from_row(self, row: dict) -> str:
        explicit = str(row.get("volatility_bucket") or row.get("volatility_mode") or "").strip()
        if explicit:
            return explicit
        atr_pct = _safe_float(row.get("atr_pct"))
        return_vol_pct = _safe_float(row.get("return_vol_pct"))
        volatility = max(atr_pct or 0.0, return_vol_pct or 0.0)
        if volatility <= 0:
            return "unknown"
        if volatility <= 0.003:
            return "low"
        if volatility >= 0.012:
            return "high"
        return "normal"


    def trade_analytics_report(self, *, exclude_dust: bool = True) -> dict:
        analytics = {
            "avg_profit_per_trade": 0.0,
            "avg_profit_per_winner": 0.0,
            "avg_loss_per_loser": 0.0,
            "trades_per_day": 0.0,
            "avg_winner": 0.0,
            "avg_loser": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "fee_gross_profit_ratio": 0.0,
            "average_holding_time_seconds": 0.0,
            "pnl_by_regime": {},
            "trades_by_regime": {},
            "winrate_by_regime": {},
            "profit_factor_by_regime": {},
            "regime_performance": {},
            "pnl_by_trade_category": {},
            "flip_trade_count": 0,
            "flip_trade_pnl": 0.0,
            "pnl_long_trades": 0.0,
            "pnl_short_trades": 0.0,
            "long_trades": 0,
            "short_trades": 0,
            "long_winrate_pct": 0.0,
            "short_winrate_pct": 0.0,
            "maker_trades": 0,
            "taker_trades": 0,
            "maker_fee_total": 0.0,
            "taker_fee_total": 0.0,
            "maker_ratio_pct": 0.0,
            "taker_ratio_pct": 0.0,
            "dust_trade_count": 0,
            "dust_volume": 0.0,
            "exclude_dust_from_performance_stats": exclude_dust,
            "avg_winner_before": 0.0,
            "avg_winner_after": 0.0,
            "gross_pnl_before_fees": 0.0,
            "total_fees": 0.0,
            "net_pnl": 0.0,
            "fee_to_gross_profit_ratio": 0.0,
            "avg_net_profit_per_closed_trade": 0.0,
            "avg_fee_per_trade": 0.0,
            "profit_factor_after_fees": 0.0,
            "net_pnl_after_fees": 0.0,
            "average_winner": 0.0,
            "average_loser": 0.0,
            "win_loss_ratio": 0.0,
            "expectancy_per_trade": 0.0,
            "long_trade_count": 0,
            "short_trade_count": 0,
            "long_pnl": 0.0,
            "short_pnl": 0.0,
            "long_profit_factor": 0.0,
            "short_profit_factor": 0.0,
            "avg_expected_profit": 0.0,
            "avg_realized_profit": 0.0,
            "avg_realized_loss": 0.0,
            "fills_under_10_usd": 0,
            "fills_under_5_usd": 0,
            "dust_cleanup_count": 0,
            "sub_10_fill_sources": {},
            "sub_5_fill_sources": {},
            "bullish_entry_pnl": 0.0,
            "bearish_entry_pnl": 0.0,
            "min_notional_blocked_count": 0,
            "anti_chop_trigger_count": 0,
            "performance_by_side": {},
            "performance_by_regime": {},
            "performance_by_hour": {},
            "performance_by_volatility_bucket": {},
            "orderbook_bullish_entries": 0,
            "orderbook_bearish_entries": 0,
            "orderbook_filtered_entries": 0,
            "avg_pnl_bullish_entries": 0.0,
            "avg_pnl_bearish_entries": 0.0,
            "pnl_when_prediction_long": 0.0,
            "pnl_when_prediction_short": 0.0,
            "pnl_when_prediction_neutral": 0.0,
            "trades_when_prediction_long": 0,
            "trades_when_prediction_short": 0,
            "trades_when_prediction_neutral": 0,
        }
        self._refresh_trades_cache()
        csv_path = self.trades_csv
        if not csv_path.exists():
            return analytics

        winners: list[float] = []
        losers: list[float] = []
        gross_profit = 0.0
        gross_loss = 0.0
        net_gross_profit = 0.0
        net_gross_loss = 0.0
        total_fees = 0.0
        net_pnl = 0.0
        trade_count = 0
        pnl_by_regime: Counter[str] = Counter()
        regime_stats: dict[str, dict] = {}
        side_stats: dict[str, dict] = {}
        hour_stats: dict[str, dict] = {}
        volatility_bucket_stats: dict[str, dict] = {}
        pnl_by_trade_category: Counter[str] = Counter()
        long_outcomes: list[float] = []
        short_outcomes: list[float] = []
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        winner_before_cutover: list[float] = []
        winner_after_cutover: list[float] = []
        cutover_ts = datetime.now(timezone.utc)
        holding_times: list[float] = []
        open_position_started_at: datetime | None = None
        previous_position_size = 0.0
        orderbook_bullish_pnls: list[float] = []
        orderbook_bearish_pnls: list[float] = []
        net_winners: list[float] = []
        net_losers: list[float] = []
        expected_profits: list[float] = []
        sub_10_sources: Counter[str] = Counter()
        sub_5_sources: Counter[str] = Counter()
        long_gross_profit = 0.0
        long_gross_loss = 0.0
        short_gross_profit = 0.0
        short_gross_loss = 0.0

        for row in self._trades_cache:
                if not self._is_valid_trade_row(row):
                    continue
                ts = _parse_iso_utc(str(row.get("timestamp", "")))
                fee = _safe_float(row.get("fee")) or 0.0
                notional = _safe_float(row.get("notional")) or 0.0
                is_dust = _is_truthy(row.get("dust_fill")) or (0.0 < notional < 1.0)
                order_source = str(row.get("order_source") or row.get("reason") or "unknown")
                if 0.0 < notional < 10.0:
                    analytics["fills_under_10_usd"] += 1
                    sub_10_sources[order_source] += 1
                if 0.0 < notional < 5.0:
                    analytics["fills_under_5_usd"] += 1
                    sub_5_sources[order_source] += 1
                if order_source == "dust_cleanup":
                    analytics["dust_cleanup_count"] += 1
                if is_dust:
                    analytics["dust_trade_count"] += 1
                    analytics["dust_volume"] += notional
                if exclude_dust and is_dust:
                    continue
                realized = _safe_float(row.get("realized_pnl_delta")) or 0.0
                expected_profit = _safe_float(row.get("expected_net_edge"))
                if expected_profit is not None:
                    expected_profits.append(expected_profit)
                net = realized - fee
                prediction_bias = str(row.get("prediction_bias") or "NEUTRAL").upper()
                if "LONG" in prediction_bias:
                    analytics["pnl_when_prediction_long"] += net
                    analytics["trades_when_prediction_long"] += 1
                elif "SHORT" in prediction_bias:
                    analytics["pnl_when_prediction_short"] += net
                    analytics["trades_when_prediction_short"] += 1
                else:
                    analytics["pnl_when_prediction_neutral"] += net
                    analytics["trades_when_prediction_neutral"] += 1
                orderbook_classification = str(row.get("orderbook_classification") or "")
                if "Bullish" in orderbook_classification:
                    analytics["orderbook_bullish_entries"] += 1
                    orderbook_bullish_pnls.append(net)
                elif "Bearish" in orderbook_classification:
                    analytics["orderbook_bearish_entries"] += 1
                    orderbook_bearish_pnls.append(net)
                total_fees += fee
                net_pnl += net
                trade_count += 1
                if ts is not None:
                    first_ts = ts if first_ts is None or ts < first_ts else first_ts
                    last_ts = ts if last_ts is None or ts > last_ts else last_ts
                liquidity = str(row.get("fill_liquidity") or "maker").lower()
                if liquidity == "taker":
                    analytics["taker_trades"] += 1
                    analytics["taker_fee_total"] += fee
                else:
                    analytics["maker_trades"] += 1
                    analytics["maker_fee_total"] += fee
                regime = str(row.get("regime") or "unknown")
                pnl_by_regime[regime] += net
                rstats = regime_stats.setdefault(regime, self._empty_regime_stats())
                rstats["trades"] += 1
                rstats["pnl"] += net
                if net > 0:
                    rstats["wins"] += 1
                    rstats["gross_profit"] += net
                elif net < 0:
                    rstats["losses"] += 1
                    rstats["gross_loss"] += abs(net)
                trade_category = str(row.get("trade_category") or "unknown")
                pnl_by_trade_category[trade_category] += net
                is_flip_trade = str(row.get("is_flip_trade", "")).lower() == "true" or trade_category == "flip"
                if is_flip_trade:
                    analytics["flip_trade_count"] += 1
                    analytics["flip_trade_pnl"] += net
                side = str(row.get("side", "")).lower()
                side_key = side or "unknown"
                self._update_group_stats(side_stats, side_key, net)
                if ts is not None:
                    self._update_group_stats(hour_stats, f"{ts.hour:02d}:00", net)
                self._update_group_stats(volatility_bucket_stats, self._volatility_bucket_from_row(row), net)
                if side == "buy":
                    analytics["long_trades"] += 1
                    analytics["pnl_long_trades"] += net
                    long_outcomes.append(net)
                    if net > 0:
                        long_gross_profit += net
                    elif net < 0:
                        long_gross_loss += abs(net)
                elif side == "sell":
                    analytics["short_trades"] += 1
                    analytics["pnl_short_trades"] += net
                    short_outcomes.append(net)
                    if net > 0:
                        short_gross_profit += net
                    elif net < 0:
                        short_gross_loss += abs(net)
                if net > 0:
                    net_gross_profit += net
                    net_winners.append(net)
                elif net < 0:
                    net_gross_loss += abs(net)
                    net_losers.append(net)
                if realized > 0:
                    winners.append(realized)
                    if ts is not None and ts < cutover_ts:
                        winner_before_cutover.append(realized)
                    else:
                        winner_after_cutover.append(realized)
                    gross_profit += realized
                elif realized < 0:
                    losers.append(realized)
                    gross_loss += abs(realized)

                position_size = _safe_float(row.get("position_size"))
                if position_size is not None and ts is not None:
                    if abs(previous_position_size) < 1e-12 and abs(position_size) > 1e-12:
                        open_position_started_at = ts
                    if open_position_started_at is not None and abs(previous_position_size) > 1e-12 and (
                        abs(position_size) < 1e-12 or previous_position_size * position_size < 0 or realized != 0
                    ):
                        holding_times.append(max((ts - open_position_started_at).total_seconds(), 0.0))
                        open_position_started_at = ts if abs(position_size) > 1e-12 else None
                    previous_position_size = position_size

        analytics["avg_winner"] = sum(winners) / len(winners) if winners else 0.0
        analytics["avg_loser"] = sum(losers) / len(losers) if losers else 0.0
        analytics["avg_profit_per_trade"] = net_pnl / trade_count if trade_count else 0.0
        analytics["avg_profit_per_winner"] = analytics["avg_winner"]
        analytics["avg_loss_per_loser"] = analytics["avg_loser"]
        analytics["profit_factor"] = gross_profit / gross_loss if gross_loss > 1e-12 else (gross_profit if gross_profit > 0 else 0.0)
        analytics["profit_factor_after_fees"] = net_gross_profit / net_gross_loss if net_gross_loss > 1e-12 else (net_gross_profit if net_gross_profit > 0 else 0.0)
        analytics["expectancy"] = net_pnl / trade_count if trade_count else 0.0
        analytics["fee_gross_profit_ratio"] = total_fees / gross_profit if gross_profit > 1e-12 else 0.0
        analytics["fee_to_gross_profit_ratio"] = analytics["fee_gross_profit_ratio"]
        analytics["gross_pnl_before_fees"] = net_pnl + total_fees
        analytics["total_fees"] = total_fees
        analytics["net_pnl"] = net_pnl
        analytics["net_pnl_after_fees"] = net_pnl
        analytics["average_winner"] = analytics["avg_winner"]
        analytics["average_loser"] = analytics["avg_loser"]
        analytics["win_loss_ratio"] = abs(analytics["avg_winner"] / analytics["avg_loser"]) if abs(analytics["avg_loser"]) > 1e-12 else (analytics["avg_winner"] if analytics["avg_winner"] > 0 else 0.0)
        analytics["expectancy_per_trade"] = analytics["expectancy"]
        analytics["long_trade_count"] = analytics["long_trades"]
        analytics["short_trade_count"] = analytics["short_trades"]
        analytics["long_pnl"] = analytics["pnl_long_trades"]
        analytics["short_pnl"] = analytics["pnl_short_trades"]
        analytics["long_profit_factor"] = long_gross_profit / long_gross_loss if long_gross_loss > 1e-12 else (long_gross_profit if long_gross_profit > 0 else 0.0)
        analytics["short_profit_factor"] = short_gross_profit / short_gross_loss if short_gross_loss > 1e-12 else (short_gross_profit if short_gross_profit > 0 else 0.0)
        analytics["avg_expected_profit"] = sum(expected_profits) / len(expected_profits) if expected_profits else 0.0
        analytics["avg_realized_profit"] = sum(net_winners) / len(net_winners) if net_winners else 0.0
        analytics["avg_realized_loss"] = sum(net_losers) / len(net_losers) if net_losers else 0.0
        analytics["sub_10_fill_sources"] = dict(sub_10_sources)
        analytics["sub_5_fill_sources"] = dict(sub_5_sources)
        analytics["avg_net_profit_per_closed_trade"] = net_pnl / trade_count if trade_count else 0.0
        analytics["avg_fee_per_trade"] = total_fees / trade_count if trade_count else 0.0
        analytics["average_holding_time_seconds"] = sum(holding_times) / len(holding_times) if holding_times else 0.0
        analytics["pnl_by_regime"] = dict(pnl_by_regime)
        self._finalize_group_stats(regime_stats)
        analytics["regime_performance"] = regime_stats
        analytics["performance_by_regime"] = regime_stats
        analytics["performance_by_side"] = self._finalize_group_stats(side_stats)
        analytics["performance_by_hour"] = self._finalize_group_stats(hour_stats)
        analytics["performance_by_volatility_bucket"] = self._finalize_group_stats(volatility_bucket_stats)
        analytics["trades_by_regime"] = {k: v["trades"] for k, v in regime_stats.items()}
        analytics["winrate_by_regime"] = {k: v["winrate_pct"] for k, v in regime_stats.items()}
        analytics["profit_factor_by_regime"] = {k: v["profit_factor"] for k, v in regime_stats.items()}
        analytics["pnl_by_trade_category"] = dict(pnl_by_trade_category)
        analytics["long_winrate_pct"] = (sum(1 for x in long_outcomes if x > 0) / len(long_outcomes) * 100.0) if long_outcomes else 0.0
        analytics["short_winrate_pct"] = (sum(1 for x in short_outcomes if x > 0) / len(short_outcomes) * 100.0) if short_outcomes else 0.0
        total_liquidity_trades = analytics["maker_trades"] + analytics["taker_trades"]
        analytics["maker_ratio_pct"] = analytics["maker_trades"] / total_liquidity_trades * 100.0 if total_liquidity_trades else 0.0
        analytics["taker_ratio_pct"] = analytics["taker_trades"] / total_liquidity_trades * 100.0 if total_liquidity_trades else 0.0
        if first_ts is not None and last_ts is not None:
            days = max((last_ts - first_ts).total_seconds() / 86400.0, 1.0)
            analytics["trades_per_day"] = trade_count / days
        analytics["avg_winner_before"] = sum(winner_before_cutover) / len(winner_before_cutover) if winner_before_cutover else 0.0
        analytics["avg_winner_after"] = sum(winner_after_cutover) / len(winner_after_cutover) if winner_after_cutover else analytics["avg_winner"]
        analytics["bullish_entry_pnl"] = sum(orderbook_bullish_pnls)
        analytics["bearish_entry_pnl"] = sum(orderbook_bearish_pnls)
        analytics["avg_pnl_bullish_entries"] = sum(orderbook_bullish_pnls) / len(orderbook_bullish_pnls) if orderbook_bullish_pnls else 0.0
        analytics["avg_pnl_bearish_entries"] = sum(orderbook_bearish_pnls) / len(orderbook_bearish_pnls) if orderbook_bearish_pnls else 0.0
        return analytics

