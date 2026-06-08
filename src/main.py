from __future__ import annotations

import json
import logging
import os
import time
import traceback
from collections import Counter
import csv
from datetime import datetime, timezone
from pathlib import Path
import fcntl

import pandas as pd

from .config import BotConfig
from .execution_engine import AccountState, LiveExecutionEngine
from .hyperliquid_client import HyperliquidClient
from .startup_validation import run_startup_validation
from .strategy_orchestrator import StrategyOrchestrator
from .telegram_handler import TelegramHandler
from .utils import append_csv, setup_logging

logger = logging.getLogger(__name__)
STATUS_FILE = Path("data/status.json")
FATAL_LOG_FILE = Path("logs/fatal_error.log")
DEFAULT_LOCK_FILE = Path("/tmp/hyperliquid_adaptive_grid_bot.lock")
TRADES_CSV = Path("logs/trades.csv")
RISK_DECISIONS_CSV = Path("logs/risk_decisions.csv")
LAST_100_REAL_TRADES_CSV = Path("logs/last_100_real_trades.csv")


def to_df(candles: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{"open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"])} for c in candles])


def _account_metrics(cfg: BotConfig, engine: LiveExecutionEngine, client: HyperliquidClient, latest_close: float) -> AccountState:
    state = engine.account_state(getattr(cfg, "default_symbol", "BTC"), latest_close)
    logger.info("account_state state_source=%s equity=%.4f position_size=%.8f position_notional=%.4f", state.state_source, state.equity, state.position_size, state.position_notional)
    return state


def calculate_daily_pnl_metrics(*, current_equity: float, daily_start_equity: float, realized_pnl_total: float, daily_start_realized_pnl: float, unrealized_pnl: float, fees_paid_total: float, daily_start_fees_paid: float) -> dict:
    daily_pnl_usd = current_equity - daily_start_equity
    daily_realized_pnl = realized_pnl_total - daily_start_realized_pnl
    daily_fees = fees_paid_total - daily_start_fees_paid
    return {
        "account_equity": current_equity,
        "daily_start_equity": daily_start_equity,
        "daily_realized_pnl": daily_realized_pnl,
        "daily_unrealized_pnl": unrealized_pnl,
        "daily_fees": daily_fees,
        "net_daily_pnl": daily_pnl_usd,
        "daily_pnl": daily_pnl_usd,
        "daily_pnl_pct": 0.0 if daily_start_equity <= 0 else daily_pnl_usd / daily_start_equity,
    }


def _write_status(payload: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_fatal_error(exc: BaseException) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    FATAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FATAL_LOG_FILE.write_text(f"[{ts}] {traceback.format_exc()}", encoding="utf-8")
    error_detail = f"{type(exc).__name__}: {exc}"
    error_text = str(exc)
    error_code = error_text.split(":", 1)[0] if error_text.startswith("live_execution_") else error_detail
    _write_status({"status": "crashed", "timestamp": ts, "last_error": error_code, "last_error_detail": error_detail})


def _acquire_single_instance_lock() -> object | None:
    lock_file = Path(os.getenv("HYPERLIQUID_BOT_LOCK_FILE", str(DEFAULT_LOCK_FILE)))
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("single_instance_lock_already_held lock_file=%s", lock_file)
        handle.close()
        return None
    handle.write(str(time.time()))
    handle.flush()
    return handle


def _derive_risk_state(strategy_status: str, reason: str) -> str:
    hard_block_reasons = {
        "emergency_stop",
        "max_position_notional",
        "max_drawdown",
        "daily_loss_limit",
        "daily_loss",
        "liquidation_risk",
        "liq_distance",
        "one_direction_exposure",
    }
    if strategy_status == "running":
        return "OK"
    if reason in hard_block_reasons:
        return "BLOCKED"
    return "STRATEGY_PAUSED"


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_last_row(csv_path: Path) -> dict:
    if not csv_path.exists():
        return {}
    last: dict = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last = row
    return last


def _is_valid_trade_row(row: dict, expected_symbol: str) -> bool:
    symbol = str(row.get("symbol", "")).upper().strip()
    if not symbol or symbol != expected_symbol.upper():
        return False
    price = _safe_float(row.get("price"))
    qty = _safe_float(row.get("qty"))
    if price is None or qty is None or qty <= 0:
        return False
    if symbol == "BTC" and price < 1000:
        return False
    return True


def _read_last_valid_trade(csv_path: Path, expected_symbol: str) -> dict:
    if not csv_path.exists():
        return {}
    last: dict = {}
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if _is_valid_trade_row(row, expected_symbol):
                last = row
    return last


def _sanitize_trade_csv_row(row: dict) -> dict:
    sanitized = {key: value for key, value in row.items() if key is not None}
    if len(sanitized) != len(row):
        logger.warning("malformed_trade_csv_row_skipped_or_sanitized")
    return sanitized


def _write_last_100_real_trades(csv_path: Path, out_path: Path, expected_symbol: str) -> None:
    if not csv_path.exists():
        return
    rows: list[dict] = []
    fields: list[str] = []
    with csv_path.open(encoding="utf-8") as f:
        for raw_row in csv.DictReader(f):
            row = _sanitize_trade_csv_row(raw_row)
            if _is_valid_trade_row(row, expected_symbol):
                rows.append(row)
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


def _risk_block_stats_window(now_ts: float, window_seconds: int = 3600) -> tuple[int, dict[str, int], str]:
    if not RISK_DECISIONS_CSV.exists():
        return 0, {}, ""
    window_start = now_ts - window_seconds
    reason_counts: Counter[str] = Counter()
    last_block_reason = ""
    blocked_count = 0
    with RISK_DECISIONS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
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


def _trade_stats_window(now_ts: float, expected_symbol: str, window_seconds: int = 3600) -> dict:
    stats = {"trades_count": 0, "buy_count": 0, "sell_count": 0, "volume_usd": 0.0, "fees": 0.0, "realized_pnl_delta": 0.0, "last_trade_ts": "", "candidate_fills_count": 0, "no_orders_touched_count": 0, "maker_trades": 0, "taker_trades": 0, "maker_fee_total": 0.0, "taker_fee_total": 0.0, "dust_trade_count": 0, "dust_volume": 0.0}
    if TRADES_CSV.exists():
        window_start = now_ts - window_seconds
        with TRADES_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if not _is_valid_trade_row(row, expected_symbol):
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



def _is_truthy(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _empty_regime_stats() -> dict:
    return {"trades": 0, "wins": 0, "losses": 0, "winrate_pct": 0.0, "pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0}


def _update_group_stats(groups: dict[str, dict], key: str, pnl: float) -> None:
    stats = groups.setdefault(key, _empty_regime_stats())
    stats["trades"] += 1
    stats["pnl"] += pnl
    if pnl > 0:
        stats["wins"] += 1
        stats["gross_profit"] += pnl
    elif pnl < 0:
        stats["losses"] += 1
        stats["gross_loss"] += abs(pnl)


def _finalize_group_stats(groups: dict[str, dict]) -> dict[str, dict]:
    for stats in groups.values():
        stats["winrate_pct"] = (stats["wins"] / stats["trades"] * 100.0) if stats["trades"] else 0.0
        stats["profit_factor"] = stats["gross_profit"] / stats["gross_loss"] if stats["gross_loss"] > 1e-12 else (stats["gross_profit"] if stats["gross_profit"] > 0 else 0.0)
    return groups


def _volatility_bucket_from_row(row: dict) -> str:
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


def _trade_analytics_report(expected_symbol: str, csv_path: Path = TRADES_CSV, *, exclude_dust: bool = True) -> dict:
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
        "min_notional_blocked_count": 0,
        "anti_chop_trigger_count": 0,
        "performance_by_side": {},
        "performance_by_regime": {},
        "performance_by_hour": {},
        "performance_by_volatility_bucket": {},
    }
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

    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not _is_valid_trade_row(row, expected_symbol):
                continue
            ts = _parse_iso_utc(str(row.get("timestamp", "")))
            fee = _safe_float(row.get("fee")) or 0.0
            notional = _safe_float(row.get("notional")) or 0.0
            is_dust = _is_truthy(row.get("dust_fill")) or (0.0 < notional < 1.0)
            if is_dust:
                analytics["dust_trade_count"] += 1
                analytics["dust_volume"] += notional
            if exclude_dust and is_dust:
                continue
            realized = _safe_float(row.get("realized_pnl_delta")) or 0.0
            net = realized - fee
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
            rstats = regime_stats.setdefault(regime, _empty_regime_stats())
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
            _update_group_stats(side_stats, side_key, net)
            if ts is not None:
                _update_group_stats(hour_stats, f"{ts.hour:02d}:00", net)
            _update_group_stats(volatility_bucket_stats, _volatility_bucket_from_row(row), net)
            if side == "buy":
                analytics["long_trades"] += 1
                analytics["pnl_long_trades"] += net
                long_outcomes.append(net)
            elif side == "sell":
                analytics["short_trades"] += 1
                analytics["pnl_short_trades"] += net
                short_outcomes.append(net)
            if net > 0:
                net_gross_profit += net
            elif net < 0:
                net_gross_loss += abs(net)
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
    analytics["avg_net_profit_per_closed_trade"] = net_pnl / trade_count if trade_count else 0.0
    analytics["avg_fee_per_trade"] = total_fees / trade_count if trade_count else 0.0
    analytics["average_holding_time_seconds"] = sum(holding_times) / len(holding_times) if holding_times else 0.0
    analytics["pnl_by_regime"] = dict(pnl_by_regime)
    _finalize_group_stats(regime_stats)
    analytics["regime_performance"] = regime_stats
    analytics["performance_by_regime"] = regime_stats
    analytics["performance_by_side"] = _finalize_group_stats(side_stats)
    analytics["performance_by_hour"] = _finalize_group_stats(hour_stats)
    analytics["performance_by_volatility_bucket"] = _finalize_group_stats(volatility_bucket_stats)
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
    return analytics


def _parse_iso_utc(ts_raw: str) -> datetime | None:
    if not ts_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_last_real_trade_ts(expected_symbol: str, fallback: str = "") -> str:
    candidates = [
        LAST_100_REAL_TRADES_CSV,
        Path("logs/real_trades.csv"),
        TRADES_CSV,
    ]
    for csv_path in candidates:
        last_trade = _read_last_valid_trade(csv_path, expected_symbol)
        ts = str(last_trade.get("timestamp", "")).strip()
        if _parse_iso_utc(ts):
            return ts
    return fallback


def _send_startup_failure_telegram(cfg: BotConfig, tg: TelegramHandler, error_code: str, exc: BaseException) -> str:
    message = "\n".join([
        "🛑 HARD STARTUP FAILURE",
        f"error: {error_code}",
        f"detail: {exc}",
        "Mode: LIVE",
        f"Network: {cfg.hl_network}",
        f"Profile: {cfg.env_profile}",
    ])
    if tg.send(message):
        logger.info("telegram_startup_failure_send_ok error_code=%s", error_code)
        return "startup_failure_ok"
    logger.warning("telegram_startup_failure_send_failed error_code=%s token_present=%s chat_id_present=%s", error_code, bool(cfg.telegram_bot_token), bool(cfg.telegram_chat_id))
    return "startup_failure_failed"


def _send_startup_telegram(cfg: BotConfig, tg: TelegramHandler) -> str:
    telegram_status = "disabled"
    if not cfg.telegram_send_startup:
        return telegram_status
    logger.info("telegram_startup_send_attempted")
    ok = tg.send("\n".join(["🚀 Bot started", "Mode: LIVE", f"Network: {cfg.hl_network}", f"Symbol: {cfg.default_symbol}", f"Profile: {cfg.env_profile}", f"Report interval: {cfg.telegram_report_interval_seconds}s"]))
    if ok:
        logger.info("telegram_startup_send_ok")
        return "startup_ok"
    logger.warning("telegram_startup_send_failed exception_type=%s http_status=%s token_present=%s chat_id_present=%s", tg.last_error.get("exception_type", ""), tg.last_error.get("http_status", ""), bool(cfg.telegram_bot_token), bool(cfg.telegram_chat_id))
    return "startup_failed"



def _update_grid_diagnostic_report(report: dict, status: dict, *, no_grid_orders_generated: bool) -> dict:
    """Update per-process grid diagnostics for live status reporting."""
    report["cycles"] = int(report.get("cycles", 0)) + 1
    if status.get("rebuild_reason") and status.get("rebuild_reason") != "none" and status.get("force_recenter"):
        report["rebuild_count"] = int(report.get("rebuild_count", 0)) + 1
    if no_grid_orders_generated:
        report["no_grid_orders_generated_count"] = int(report.get("no_grid_orders_generated_count", 0)) + 1
    orchestrator_orphan_count = int(status.get("orphan_order_cleanup_count", 0) or 0)
    if orchestrator_orphan_count:
        report["orphan_cleanup_count"] = max(int(report.get("orphan_cleanup_count", 0)), orchestrator_orphan_count)
    elif status.get("orphan_order_detected"):
        report["orphan_cleanup_count"] = int(report.get("orphan_cleanup_count", 0)) + 1
    levels = status.get("final_effective_levels", status.get("grid_levels"))
    if levels is not None:
        distribution = dict(report.get("effective_grid_levels_distribution", {}))
        key = str(levels)
        distribution[key] = int(distribution.get(key, 0)) + 1
        report["effective_grid_levels_distribution"] = distribution
    report["last_reduction_reason"] = status.get("grid_level_reduction_reason") or status.get("reduction_reason")
    report["last_no_grid_orders_generated_reason"] = status.get("no_grid_orders_generated_reason")
    report["last_rebuild_reason"] = status.get("rebuild_reason")
    return report

def run() -> None:
    cfg = BotConfig.from_env()
    setup_logging(cfg.log_level)
    logger.info("Startup profile=%s execution_mode=live_only live_execution_enabled=%s", cfg.env_profile, cfg.live_execution_enabled)
    logger.info("risk_settings max_position_notional_usd=%s soft_exposure_cap_pct=%s hard_exposure_cap_pct=%s absolute_exposure_cap_pct=%s execution_mode=live_only enable_live_trading=%s live_execution_enabled=%s", cfg.max_position_notional_usd, cfg.soft_exposure_cap_pct, cfg.hard_exposure_cap_pct, cfg.absolute_exposure_cap_pct, cfg.enable_live_trading, cfg.live_execution_enabled)
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)
    try:
        run_startup_validation(cfg)
    except RuntimeError as exc:
        error_text = str(exc)
        error_code = error_text.split(":", 1)[0] if error_text.startswith("live_execution_") else type(exc).__name__
        _send_startup_failure_telegram(cfg, tg, error_code, exc)
        _write_status({
            "status": "startup_failed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "execution_mode": "live_only",
            "enable_live_trading": cfg.enable_live_trading,
            "live_execution_enabled": cfg.live_execution_enabled,
            "last_error": error_code,
            "last_error_detail": str(exc),
        })
        raise

    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()
    engine = LiveExecutionEngine(client=client, state_file=cfg.state_file, start_balance=0.0, max_position_notional_usd=cfg.max_position_notional_usd, allow_position_flip=cfg.allow_position_flip, max_notional_per_trade_usd=cfg.max_notional_per_trade_usd, min_notional_usd=cfg.min_notional_usd, min_order_notional_usd=cfg.min_order_notional_usd, dust_position_notional_usd=cfg.dust_position_notional_usd, leverage=cfg.base_leverage)
    logger.info("execution_mode=live_only")
    client.set_leverage(cfg.default_symbol, cfg.base_leverage)
    orchestrator = StrategyOrchestrator(config=cfg, execution_engine=engine)
    current_day = datetime.now(timezone.utc).date()
    if engine.state.current_day:
        current_day = datetime.fromisoformat(engine.state.current_day).date()
    daily_start_equity = engine.state.daily_start_equity
    daily_start_realized_pnl = engine.state.daily_start_realized_pnl
    daily_start_fees_paid = engine.state.daily_start_fees_paid

    last_telegram_report_ts = 0.0
    last_pause_reason = ""
    last_risk_state_value = ""
    last_hard_risk_alert_ts: dict[str, float] = {}
    last_recovery_alert_ts = 0.0
    grid_diagnostic_report = {
        "cycles": 0,
        "rebuild_count": 0,
        "no_grid_orders_generated_count": 0,
        "orphan_cleanup_count": 0,
        "effective_grid_levels_distribution": {},
    }

    telegram_status = _send_startup_telegram(cfg, tg)

    while True:
        buys: list[dict] = []
        sells: list[dict] = []
        candles = to_df(client.get_candles(cfg.default_symbol, lookback=200))
        latest_close = float(candles["close"].iloc[-1])
        account_state = _account_metrics(cfg, engine, client, latest_close)
        unrealized_pnl, equity, position_notional = account_state.unrealized_pnl, account_state.equity, account_state.position_notional
        now_day = datetime.now(timezone.utc).date()
        if daily_start_equity <= 0 or not engine.state.current_day:
            daily_start_equity = equity
            daily_start_realized_pnl = account_state.realized_pnl
            daily_start_fees_paid = account_state.fees_paid
            engine.state.current_day = now_day.isoformat()
            engine.state.daily_start_equity = daily_start_equity
            engine.state.daily_start_realized_pnl = daily_start_realized_pnl
            engine.state.daily_start_fees_paid = daily_start_fees_paid
            engine.save_state()
        if now_day != current_day:
            current_day = now_day
            daily_start_equity = equity
            daily_start_realized_pnl = account_state.realized_pnl
            daily_start_fees_paid = account_state.fees_paid
            engine.state.current_day = current_day.isoformat()
            engine.state.daily_start_equity = daily_start_equity
            engine.state.daily_start_realized_pnl = daily_start_realized_pnl
            engine.state.daily_start_fees_paid = daily_start_fees_paid
            engine.save_state()
        daily_metrics = calculate_daily_pnl_metrics(current_equity=equity, daily_start_equity=daily_start_equity, realized_pnl_total=account_state.realized_pnl, daily_start_realized_pnl=daily_start_realized_pnl, unrealized_pnl=unrealized_pnl, fees_paid_total=account_state.fees_paid, daily_start_fees_paid=daily_start_fees_paid)
        daily_pnl_pct = daily_metrics["daily_pnl_pct"]

        status = orchestrator.on_tick(candles, equity=equity, daily_pnl_pct=daily_pnl_pct, symbol=cfg.default_symbol, position_notional=position_notional)
        risk = status.get("risk")
        reason = status.get("reason") or (getattr(risk, "reason", "") if risk else "")
        strategy_status = status.get("status", "unknown")
        risk_state = _derive_risk_state(strategy_status, reason)
        mode = status.get("mode", "reduce_only" if reason == "reduce_only_requested" else "neutral")

        latest_candle = candles.iloc[-1].to_dict()
        latest_candle["symbol"] = cfg.default_symbol
        fills = engine.on_candle(latest_candle, regime=status.get("regime", "unknown"), mode=mode, risk_state=risk_state, pause_reason=reason, strategy_status=strategy_status)
        trade_events = engine.consume_trade_log()
        account_state = _account_metrics(cfg, engine, client, latest_close)
        unrealized_pnl, equity, position_notional = account_state.unrealized_pnl, account_state.equity, account_state.position_notional
        daily_metrics = calculate_daily_pnl_metrics(current_equity=equity, daily_start_equity=daily_start_equity, realized_pnl_total=account_state.realized_pnl, daily_start_realized_pnl=daily_start_realized_pnl, unrealized_pnl=unrealized_pnl, fees_paid_total=account_state.fees_paid, daily_start_fees_paid=daily_start_fees_paid)

        for tr in trade_events:
            logger.info("trade_event side=%s symbol=%s price=%.6f qty=%.8f", tr["side"], tr["symbol"], tr["price"], tr["qty"])
            if cfg.telegram_send_fills:
                msg = tg.format_fill_alert(tr, mode_name="LIVE")
                if tg.send(msg):
                    logger.info("telegram_fill_alert_sent side=%s", str(tr.get("side", "")).upper())
                else:
                    logger.warning("telegram_fill_alert_failed error=send_returned_false side=%s", str(tr.get("side", "")).upper())
        last_trade_dt = engine.last_real_trade_ts

        hard_risk_reasons = {"emergency_stop", "max_drawdown", "daily_loss", "daily_loss_limit", "max_position_notional", "liquidation_risk", "liq_distance"}
        now_ts = time.time()
        if cfg.telegram_send_hard_risk_alerts and risk_state == "BLOCKED" and reason in hard_risk_reasons:
            prev_ts = last_hard_risk_alert_ts.get(reason, 0.0)
            if now_ts - prev_ts >= cfg.telegram_risk_alert_cooldown_seconds:
                tg.send(f"🛡 HARD RISK BLOCK\nreason: {reason}\nrisk_state: {risk_state}\npause_reason: {reason or 'none'}")
                last_hard_risk_alert_ts[reason] = now_ts
        if last_risk_state_value == "BLOCKED" and risk_state == "OK" and (now_ts - last_recovery_alert_ts) >= cfg.telegram_risk_alert_cooldown_seconds:
            tg.send("✅ Risk recovered: OK")
            last_recovery_alert_ts = now_ts
        last_risk_state_value = risk_state
        last_pause_reason = reason

        append_csv("logs/equity_curve.csv", [time.time(), equity, account_state.equity, account_state.realized_pnl, unrealized_pnl, account_state.fees_paid], ["ts", "equity", "cash", "realized_pnl", "unrealized_pnl", "fees_paid"])

        status_payload = {
            "status": strategy_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "execution_mode": "live_only",
            "state_source": account_state.state_source,
            "symbol": cfg.default_symbol,
            "price": latest_close,
            "regime": status.get("regime", "unknown"),
            "mode": mode,
            "order_size": status.get("order_size", 0.0),
            "order_notional": status.get("order_notional", 0.0),
            "position_size": account_state.position_size,
            "position_notional": position_notional,
            "cash": account_state.equity,
            "realized_pnl": account_state.realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "fees_paid": account_state.fees_paid,
            "equity": equity,
            "daily_pnl_pct": daily_pnl_pct,
            "risk_state": risk_state,
            "strategy_status": strategy_status,
            "pause_reason": reason,
            "last_error": "",
            "telegram_status": telegram_status,
        }

        last_trade = _read_last_valid_trade(TRADES_CSV, cfg.default_symbol)
        try:
            _write_last_100_real_trades(TRADES_CSV, LAST_100_REAL_TRADES_CSV, cfg.default_symbol)
        except Exception:
            logger.exception("last_100_real_trades_export_failed")
        blocked_1h, blocked_by_reason_1h, last_block_reason = _risk_block_stats_window(now_ts=time.time())
        trade_stats_1h = _trade_stats_window(now_ts=time.time(), expected_symbol=cfg.default_symbol)
        trade_analytics = _trade_analytics_report(cfg.default_symbol, exclude_dust=cfg.exclude_dust_from_performance_stats)
        position_side = "FLAT"
        if account_state.position_size > 0:
            position_side = "LONG"
        elif account_state.position_size < 0:
            position_side = "SHORT"
        effective_position_side = status.get("effective_position_side", position_side)
        is_dust_position = bool(status.get("is_dust_position", False))
        dust_threshold_usd = float(status.get("dust_threshold_usd", 1.0))
        status_payload.update(
            {
                "position_side": position_side,
                "effective_position_side": effective_position_side,
                "is_dust_position": is_dust_position,
                "dust_threshold_usd": dust_threshold_usd,
                "position_notional_raw": status.get("position_notional_raw", position_notional),
                "effective_position_notional": status.get("effective_position_notional", position_notional),
                "last_recenter_ts": status.get("last_recenter_ts"),
                "seconds_since_recenter": status.get("seconds_since_recenter"),
                "recenter_cooldown_seconds": status.get("recenter_cooldown_seconds"),
                "recenter_requested": bool(status.get("recenter_requested", False)),
                "recenter_blocked_by_cooldown": bool(status.get("recenter_blocked_by_cooldown", False)),
                "force_recenter": bool(status.get("force_recenter", False)),
                "in_position_for_recenter": bool(status.get("in_position_for_recenter", False)),
                "forced_rebuild": bool(status.get("forced_rebuild", False)),
                "rebuild_reason": status.get("rebuild_reason"),
                "grid_spacing_pct": status.get("grid_spacing_pct"),
                "spacing_source": status.get("spacing_source"),
                "atr_pct": status.get("atr_pct"),
                "grid_levels": status.get("grid_levels"),
                "configured_levels": status.get("configured_levels", cfg.grid_levels),
                "volatility_grid_levels": status.get("volatility_grid_levels"),
                "risk_adjusted_levels": status.get("risk_adjusted_levels"),
                "final_effective_levels": status.get("final_effective_levels", status.get("grid_levels")),
                "reduction_reason": status.get("grid_level_reduction_reason", status.get("reduction_reason")),
                "grid_level_reduction_reason": status.get("grid_level_reduction_reason"),
                "no_grid_orders_generated_reason": status.get("no_grid_orders_generated_reason"),
                "regime_confidence": status.get("regime_confidence"),
                "trend_bias": status.get("trend_bias"),
                "trend_transition_score": status.get("trend_transition_score"),
                "transition_direction": status.get("transition_direction"),
                "transition_confidence": status.get("transition_confidence"),
                "trend_transition_delta": status.get("trend_transition_delta"),
                "higher_high_sequence_score": status.get("higher_high_sequence_score"),
                "higher_low_sequence_score": status.get("higher_low_sequence_score"),
                "ema_slope_acceleration_score": status.get("ema_slope_acceleration_score"),
                "momentum_acceleration_score": status.get("momentum_acceleration_score"),
                "market_stress_enabled": status.get("market_stress_enabled"),
                "volatility_mode": status.get("volatility_mode"),
                "market_stress_score": status.get("market_stress_score"),
                "trend_strength_score": status.get("trend_strength_score"),
                "opportunity_mode": status.get("opportunity_mode"),
                "btc_5m_return_pct": status.get("btc_5m_return_pct"),
                "btc_1h_return_pct": status.get("btc_1h_return_pct"),
                "market_stress_reason": status.get("market_stress_reason"),
                "mark_price": latest_close,
                "last_trade_price": _safe_float(last_trade.get("price")),
                "last_trade_qty": _safe_float(last_trade.get("qty")),
                "last_trade_notional": _safe_float(last_trade.get("notional")),
                "last_trade_fee": _safe_float(last_trade.get("fee")),
                "last_trade_realized_pnl": _safe_float(last_trade.get("realized_pnl_delta")),
                "last_trade_reason": last_trade.get("reason", ""),
                "last_trade_side": last_trade.get("side", ""),
                "blocked_risk_decisions_1h": blocked_1h,
                "blocked_risk_decisions_by_reason_1h": blocked_by_reason_1h,
                "blocked_risk_decisions_30m": blocked_1h,
                "blocked_risk_decisions_by_reason": blocked_by_reason_1h,
                "last_block_reason": last_block_reason,
                "allowed_to_trade": bool(status.get("allowed_to_trade", risk_state == "OK")),
                "allowed_to_reduce": bool(status.get("allowed_to_reduce", abs(account_state.position_size) > 1e-12)),
                "position_management_action": status.get("position_management_action", "none"),
                "total_pnl": account_state.realized_pnl + unrealized_pnl,
                "total_pnl_pct": ((account_state.realized_pnl + unrealized_pnl) / daily_start_equity) if daily_start_equity > 0 else None,
                "account_equity": daily_metrics["account_equity"],
                "daily_start_equity": daily_metrics["daily_start_equity"],
                "daily_realized_pnl": daily_metrics["daily_realized_pnl"],
                "daily_unrealized_pnl": daily_metrics["daily_unrealized_pnl"],
                "daily_fees": daily_metrics["daily_fees"],
                "net_daily_pnl": daily_metrics["net_daily_pnl"],
                "daily_pnl": daily_metrics["daily_pnl"],
                "daily_pnl_pct": daily_metrics["daily_pnl_pct"],
                "unrealized_pnl_pct": ((unrealized_pnl / abs(account_state.avg_entry * account_state.position_size)) if abs(account_state.avg_entry * account_state.position_size) > 1e-9 else 0.0),
                "exposure_pct": (position_notional / equity) if equity > 1e-9 else 0.0,
                "avg_entry": account_state.avg_entry,
                "trades_1h": trade_stats_1h["trades_count"],
                "buy_count_1h": trade_stats_1h["buy_count"],
                "sell_count_1h": trade_stats_1h["sell_count"],
                "volume_usd_1h": trade_stats_1h["volume_usd"],
                "fees_1h": trade_stats_1h["fees"],
                "realized_pnl_delta_1h": trade_stats_1h["realized_pnl_delta"],
                "maker_trades_1h": trade_stats_1h["maker_trades"],
                "taker_trades_1h": trade_stats_1h["taker_trades"],
                "maker_fee_total_1h": trade_stats_1h["maker_fee_total"],
                "taker_fee_total_1h": trade_stats_1h["taker_fee_total"],
                "dust_trade_count_1h": trade_stats_1h["dust_trade_count"],
                "dust_volume_1h": trade_stats_1h["dust_volume"],
                "trades_30m": trade_stats_1h["trades_count"],
                "buy_count_30m": trade_stats_1h["buy_count"],
                "sell_count_30m": trade_stats_1h["sell_count"],
                "volume_usd_30m": trade_stats_1h["volume_usd"],
                "fees_30m": trade_stats_1h["fees"],
                "realized_pnl_delta_30m": trade_stats_1h["realized_pnl_delta"],
                "no_fill_cycles": engine.no_fill_cycles,
                "avg_profit_per_trade": trade_analytics["avg_profit_per_trade"],
                "avg_profit_per_winner": trade_analytics["avg_profit_per_winner"],
                "avg_loss_per_loser": trade_analytics["avg_loss_per_loser"],
                "trades_per_day": trade_analytics["trades_per_day"],
                "avg_winner": trade_analytics["avg_winner"],
                "avg_loser": trade_analytics["avg_loser"],
                "profit_factor": trade_analytics["profit_factor"],
                "expectancy": trade_analytics["expectancy"],
                "fee_gross_profit_ratio": trade_analytics["fee_gross_profit_ratio"],
                "average_holding_time_seconds": trade_analytics["average_holding_time_seconds"],
                "pnl_by_regime": trade_analytics["pnl_by_regime"],
                "trades_by_regime": trade_analytics["trades_by_regime"],
                "winrate_by_regime": trade_analytics["winrate_by_regime"],
                "profit_factor_by_regime": trade_analytics["profit_factor_by_regime"],
                "regime_performance": trade_analytics["regime_performance"],
                "pnl_by_trade_category": trade_analytics["pnl_by_trade_category"],
                "flip_trade_count": trade_analytics["flip_trade_count"],
                "flip_trade_pnl": trade_analytics["flip_trade_pnl"],
                "pnl_long_trades": trade_analytics["pnl_long_trades"],
                "pnl_short_trades": trade_analytics["pnl_short_trades"],
                "long_trades": trade_analytics["long_trades"],
                "short_trades": trade_analytics["short_trades"],
                "long_winrate_pct": trade_analytics["long_winrate_pct"],
                "short_winrate_pct": trade_analytics["short_winrate_pct"],
                "maker_trades": trade_analytics["maker_trades"],
                "taker_trades": trade_analytics["taker_trades"],
                "maker_fee_total": trade_analytics["maker_fee_total"],
                "taker_fee_total": trade_analytics["taker_fee_total"],
                "maker_ratio_pct": trade_analytics["maker_ratio_pct"],
                "taker_ratio_pct": trade_analytics["taker_ratio_pct"],
                "dust_trade_count": trade_analytics["dust_trade_count"],
                "dust_volume": trade_analytics["dust_volume"],
                "exclude_dust_from_performance_stats": trade_analytics["exclude_dust_from_performance_stats"],
                "avg_winner_before": trade_analytics["avg_winner_before"],
                "avg_winner_after": trade_analytics["avg_winner_after"],
                "gross_pnl_before_fees": trade_analytics["gross_pnl_before_fees"],
                "total_fees": trade_analytics["total_fees"],
                "net_pnl": trade_analytics["net_pnl"],
                "fee_to_gross_profit_ratio": trade_analytics["fee_to_gross_profit_ratio"],
                "avg_net_profit_per_closed_trade": trade_analytics["avg_net_profit_per_closed_trade"],
                "avg_fee_per_trade": trade_analytics["avg_fee_per_trade"],
                "profit_factor_after_fees": trade_analytics["profit_factor_after_fees"],
                "min_notional_blocked_count": max(int(status.get("min_notional_blocked_count", 0) or 0), int(getattr(engine, "min_notional_blocked_count", 0) or 0)),
                "anti_chop_trigger_count": status.get("anti_chop_trigger_count", 0),
                "anti_chop_cooldown_active": status.get("anti_chop_cooldown_active", False),
                "anti_chop_cooldown_until": status.get("anti_chop_cooldown_until", ""),
                "edge_filter_skipped_orders": status.get("edge_filter_skipped_orders", 0),
                "edge_filter_skipped_by_reason": status.get("edge_filter_skipped_by_reason", {}),
                "expected_move_pct": status.get("expected_move_pct"),
                "expected_gross_pnl": status.get("expected_gross_pnl"),
                "expected_fee_cost": status.get("expected_fee_cost"),
                "expected_net_edge": status.get("expected_net_edge"),
                "expected_net_edge_pct": status.get("expected_net_edge_pct"),
                "expected_rr_ratio": status.get("expected_rr_ratio"),
                "orphan_order_cleanup_count": status.get("orphan_order_cleanup_count", 0),
                "stale_order_cleanup_count": status.get("stale_order_cleanup_count", 0),
                "orphan_order_detected": bool(status.get("orphan_order_detected", False)),
                "stale_order_detected": bool(status.get("stale_order_detected", False)),
                "cleanup_canceled_orders": status.get("cleanup_canceled_orders", 0),
                "state_uncertain_skip_cycle": bool(status.get("state_uncertain_skip_cycle", False)),
            }
        )
        last_trade_ts = _resolve_last_real_trade_ts(cfg.default_symbol, fallback=(engine.last_real_trade_ts.isoformat() if engine.last_real_trade_ts else ""))
        last_trade_dt = _parse_iso_utc(str(last_trade_ts))
        last_trade_age_hours = ((datetime.now(timezone.utc) - last_trade_dt).total_seconds() / 3600) if last_trade_dt else None
        mark_price = latest_close
        buys = sorted([o for o in engine.open_orders if str(o.get("symbol", "")).upper() == cfg.default_symbol.upper() and str(o.get("side", "")).lower() == "buy"], key=lambda x: float(x.get("price", 0.0)), reverse=True)
        sells = sorted([o for o in engine.open_orders if str(o.get("symbol", "")).upper() == cfg.default_symbol.upper() and str(o.get("side", "")).lower() == "sell"], key=lambda x: float(x.get("price", 0.0)))
        no_grid_orders_generated = not buys and not sells
        if no_grid_orders_generated:
            logger.info(
                "no_grid_orders_generated reason=%s status=%s pause_reason=%s configured_levels=%s volatility_grid_levels=%s risk_adjusted_levels=%s final_effective_levels=%s reduction_reason=%s allowed_to_trade=%s force_recenter=%s rebuild_reason=%s state_uncertain_skip_cycle=%s",
                status_payload.get("no_grid_orders_generated_reason") or "no_active_exchange_orders",
                strategy_status,
                reason or "none",
                status_payload.get("configured_levels"),
                status_payload.get("volatility_grid_levels"),
                status_payload.get("risk_adjusted_levels"),
                status_payload.get("final_effective_levels"),
                status_payload.get("reduction_reason"),
                status_payload.get("allowed_to_trade"),
                status_payload.get("force_recenter"),
                status_payload.get("rebuild_reason"),
                status_payload.get("state_uncertain_skip_cycle"),
            )
        grid_diagnostic_report = _update_grid_diagnostic_report(grid_diagnostic_report, status_payload, no_grid_orders_generated=no_grid_orders_generated)
        status_payload["grid_diagnostic_report"] = dict(grid_diagnostic_report)
        next_order = None
        if buys:
            next_order = {"side": "buy", "price": float(buys[0]["price"])}
        if sells and (next_order is None or abs(float(sells[0]["price"]) - mark_price) < abs(next_order["price"] - mark_price)):
            next_order = {"side": "sell", "price": float(sells[0]["price"])}
        next_exit_side = None
        next_exit_price = None
        if account_state.position_size > 0 and sells:
            next_exit_side = "sell"
            next_exit_price = float(sells[0]["price"])
        elif account_state.position_size < 0 and buys:
            next_exit_side = "buy"
            next_exit_price = float(buys[0]["price"])

        status_payload.update({"last_real_trade_ts": last_trade_ts, "last_real_trade_age_hours": last_trade_age_hours, "next_order_side": next_order["side"] if next_order else None, "next_order_price": next_order["price"] if next_order else None, "next_exit_side": next_exit_side, "next_exit_price": next_exit_price, "take_profit_price": next_exit_price, "stop_price": None, "trailing_stop_price": None})
        nearest_buy = float(buys[0]["price"]) if buys else None
        nearest_sell = float(sells[0]["price"]) if sells else None
        status_payload.update(
            {
                "nearest_buy_price": nearest_buy,
                "nearest_sell_price": nearest_sell,
                "distance_to_buy_pct": ((mark_price - nearest_buy) / mark_price) if nearest_buy else None,
                "distance_to_sell_pct": ((nearest_sell - mark_price) / mark_price) if nearest_sell else None,
                "active_buy_orders": len(buys),
                "active_sell_orders": len(sells),
                "one_sided_grid_due_to_dust": bool(is_dust_position and ((len(buys) == 0) ^ (len(sells) == 0)) and (len(buys) + len(sells) > 0)),
                "open_orders": len(engine.open_orders),
            }
        )
        _write_status(status_payload)
        if cfg.telegram_enable_commands:
            for update in tg.fetch_commands():
                text = str(update.get("message", {}).get("text", "")).strip()
                if not text.startswith("/"):
                    continue
                if text.startswith("/help"):
                    tg.send("/status - aktuális státusz\n/risk - risk állapot\n/trades - utolsó 5 trade\n/help - parancsok")
                elif text.startswith("/status"):
                    tg.send(tg.format_status_report({**status_payload, "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}))
                elif text.startswith("/risk"):
                    tg.send(f"risk_state: {status_payload.get('risk_state')}\npause_reason: {status_payload.get('pause_reason') or 'none'}\nlast_block_reason: {status_payload.get('last_block_reason') or 'none'}\nblocked_risk_decisions_1h: {status_payload.get('blocked_risk_decisions_1h', 0)}\nallowed_to_trade: {status_payload.get('allowed_to_trade')}")
                elif text.startswith("/trades"):
                    lines = ["Utolsó 5 trade:"]
                    rows = []
                    if TRADES_CSV.exists():
                        with TRADES_CSV.open(encoding="utf-8") as f:
                            rows = [r for r in csv.DictReader(f)][-5:]
                    for r in rows:
                        lines.append(f"{r.get('timestamp','')} | {r.get('side','').upper()} {r.get('qty','')} @ {r.get('price','')} | pnlΔ {r.get('realized_pnl_delta','0')}")
                    tg.send("\n".join(lines))

        now_ts = time.time()
        if cfg.telegram_send_periodic_status and (now_ts - last_telegram_report_ts) >= cfg.telegram_report_interval_seconds:
            tg.send_status_report({**status_payload, "status": status.get("status", "unknown"), "network": cfg.hl_network, "mode_name": "LIVE", "grid_mode": mode, "risk_reason": reason or "none", "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")})
            last_telegram_report_ts = now_ts

        engine.state.current_day = current_day.isoformat()
        engine.state.daily_start_equity = daily_start_equity
        engine.state.daily_start_realized_pnl = daily_start_realized_pnl
        engine.state.daily_start_fees_paid = daily_start_fees_paid
        engine.save_state()
        time.sleep(cfg.tick_seconds)


if __name__ == "__main__":
    lock_handle = _acquire_single_instance_lock()
    if lock_handle is None:
        raise SystemExit(0)
    try:
        run()
    except Exception as exc:
        logger.exception("fatal_exception_in_main")
        _write_fatal_error(exc)
        raise
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
