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
from .execution_engine import ExecutionEngine
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


def _paper_metrics(cfg: BotConfig, engine: ExecutionEngine, client: HyperliquidClient, latest_close: float) -> tuple[float, float, float]:
    unrealized_pnl = engine.unrealized_pnl(latest_close) if cfg.paper_mode else 0.0
    equity = engine.equity(latest_close) if cfg.paper_mode else client.get_balance().get("equity", cfg.paper_start_balance_usd)
    position_notional = abs(engine.paper.position_size * latest_close) if cfg.paper_mode else 0.0
    return unrealized_pnl, equity, position_notional


def _write_status(payload: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_fatal_error(exc: BaseException) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    FATAL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FATAL_LOG_FILE.write_text(f"[{ts}] {traceback.format_exc()}", encoding="utf-8")
    _write_status({"status": "crashed", "timestamp": ts, "last_error": f"{type(exc).__name__}: {exc}"})


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


def _write_last_100_real_trades(csv_path: Path, out_path: Path, expected_symbol: str) -> None:
    if not csv_path.exists():
        return
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if _is_valid_trade_row(row, expected_symbol):
                rows.append(row)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else ["timestamp", "symbol", "side", "price", "qty", "notional", "fee", "realized_pnl_delta", "realized_pnl_total", "cash", "equity", "position_size", "position_notional", "regime", "mode", "risk_state", "pause_reason", "reason"]
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
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
    stats = {"trades_count": 0, "buy_count": 0, "sell_count": 0, "volume_usd": 0.0, "fees": 0.0, "realized_pnl_delta": 0.0, "last_trade_ts": "", "candidate_fills_count": 0, "no_orders_touched_count": 0}
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
                stats["volume_usd"] += _safe_float(row.get("notional")) or 0.0
                stats["fees"] += _safe_float(row.get("fee")) or 0.0
                stats["realized_pnl_delta"] += _safe_float(row.get("realized_pnl_delta")) or 0.0
                stats["last_trade_ts"] = row.get("timestamp", "")
    return stats


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


def _send_startup_telegram(cfg: BotConfig, tg: TelegramHandler) -> str:
    telegram_status = "disabled"
    if not cfg.telegram_send_startup:
        return telegram_status
    logger.info("telegram_startup_send_attempted")
    ok = tg.send("\n".join(["🚀 Bot started", f"Mode: {'PAPER' if cfg.paper_mode else 'LIVE'}", f"Network: {cfg.hl_network}", f"Symbol: {cfg.default_symbol}", f"Profile: {cfg.env_profile}", f"Fill model: {cfg.fill_model}", f"Report interval: {cfg.telegram_report_interval_seconds}s"]))
    if ok:
        logger.info("telegram_startup_send_ok")
        return "startup_ok"
    logger.warning("telegram_startup_send_failed exception_type=%s http_status=%s token_present=%s chat_id_present=%s", tg.last_error.get("exception_type", ""), tg.last_error.get("http_status", ""), bool(cfg.telegram_bot_token), bool(cfg.telegram_chat_id))
    return "startup_failed"


def run() -> None:
    cfg = BotConfig.from_env()
    setup_logging(cfg.log_level)
    logger.info("Startup profile=%s paper_mode=%s fill_model=%s", cfg.env_profile, cfg.paper_mode, cfg.fill_model)
    logger.info("risk_settings max_position_notional_usd=%s soft_exposure_cap_pct=%s hard_exposure_cap_pct=%s absolute_exposure_cap_pct=%s paper_mode=%s enable_live_trading=%s", cfg.max_position_notional_usd, cfg.soft_exposure_cap_pct, cfg.hard_exposure_cap_pct, cfg.absolute_exposure_cap_pct, cfg.paper_mode, cfg.enable_live_trading)
    run_startup_validation(cfg)

    client = HyperliquidClient(cfg.private_key, cfg.account_address, cfg.hl_network)
    client.connect()
    engine = ExecutionEngine(client=client, state_file=cfg.state_file, paper_mode=cfg.paper_mode, enable_live_trading=cfg.enable_live_trading, start_balance=cfg.paper_start_balance_usd, fill_model=cfg.fill_model, max_position_notional_usd=cfg.max_position_notional_usd, soft_exposure_cap_pct=cfg.soft_exposure_cap_pct, hard_exposure_cap_pct=cfg.hard_exposure_cap_pct, absolute_exposure_cap_pct=cfg.absolute_exposure_cap_pct, allow_position_flip=cfg.allow_position_flip)
    orchestrator = StrategyOrchestrator(config=cfg, execution_engine=engine)
    tg = TelegramHandler(cfg.telegram_bot_token, cfg.telegram_chat_id)
    current_day = datetime.now(timezone.utc).date()
    if engine.paper.current_day:
        current_day = datetime.fromisoformat(engine.paper.current_day).date()
    daily_start_equity = engine.paper.daily_start_equity if engine.paper.daily_start_equity > 0 else cfg.paper_start_balance_usd

    last_telegram_report_ts = 0.0
    last_pause_reason = ""
    last_risk_state_value = ""
    last_hard_risk_alert_ts: dict[str, float] = {}
    last_recovery_alert_ts = 0.0

    telegram_status = _send_startup_telegram(cfg, tg)

    while True:
        buys: list[dict] = []
        sells: list[dict] = []
        candles = to_df(client.get_candles(cfg.default_symbol, lookback=200))
        latest_close = float(candles["close"].iloc[-1])
        unrealized_pnl, equity, position_notional = _paper_metrics(cfg, engine, client, latest_close)
        now_day = datetime.now(timezone.utc).date()
        if now_day != current_day:
            current_day = now_day
            daily_start_equity = equity
            engine.paper.current_day = current_day.isoformat()
            engine.paper.daily_start_equity = daily_start_equity
            engine.save_state()
        daily_pnl_pct = 0.0 if daily_start_equity <= 0 else (equity - daily_start_equity) / daily_start_equity

        status = orchestrator.on_tick(candles, equity=equity, daily_pnl_pct=daily_pnl_pct, symbol=cfg.default_symbol, position_notional=position_notional)
        risk = status.get("risk")
        reason = status.get("reason") or (getattr(risk, "reason", "") if risk else "")
        strategy_status = status.get("status", "unknown")
        risk_state = _derive_risk_state(strategy_status, reason)
        mode = status.get("mode", "reduce_only" if reason == "reduce_only_requested" else "neutral")

        fills = engine.on_candle(candles.iloc[-1].to_dict(), regime=status.get("regime", "unknown"), mode=mode, risk_state=risk_state, pause_reason=reason, strategy_status=strategy_status)
        trade_events = engine.consume_trade_log()
        unrealized_pnl, equity, position_notional = _paper_metrics(cfg, engine, client, latest_close)

        for tr in trade_events:
            logger.info("trade_event side=%s symbol=%s price=%.6f qty=%.8f", tr["side"], tr["symbol"], tr["price"], tr["qty"])
            if cfg.telegram_send_fills:
                msg = tg.format_fill_alert(tr, mode_name="PAPER" if cfg.paper_mode else "LIVE")
                if tg.send(msg):
                    logger.info("telegram_fill_alert_sent side=%s", str(tr.get("side", "")).upper())
                else:
                    logger.warning("telegram_fill_alert_failed error=send_returned_false side=%s", str(tr.get("side", "")).upper())
        last_trade_dt = engine.last_real_trade_ts
        if last_trade_dt and abs(engine.paper.position_size) > 1e-12:
            age_hours = (datetime.now(timezone.utc) - last_trade_dt).total_seconds() / 3600
            if age_hours > cfg.stale_position_max_hours and position_notional > cfg.min_residual_notional_usd:
                close_side = "sell" if engine.paper.position_size > 0 else "buy"
                close_qty = abs(engine.paper.position_size)
                logger.warning("stale_position_cleanup_triggered side=%s close_qty=%s position_notional_before=%.4f reason=stale_position age_hours=%.3f", close_side, close_qty, position_notional, age_hours)
                engine._apply_fill({"symbol": cfg.default_symbol, "side": close_side, "price": latest_close, "size": close_qty}, reason="stale_position_cleanup", regime=status.get("regime", "unknown"), mode=mode, risk_state="OK", pause_reason="")
                trade_events.extend(engine.consume_trade_log())

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

        append_csv("logs/equity_curve.csv", [time.time(), equity, engine.paper.cash, engine.paper.realized_pnl, unrealized_pnl, engine.paper.fees_paid], ["ts", "equity", "cash", "realized_pnl", "unrealized_pnl", "fees_paid"])

        status_payload = {
            "status": strategy_status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper_mode": cfg.paper_mode,
            "symbol": cfg.default_symbol,
            "price": latest_close,
            "regime": status.get("regime", "unknown"),
            "mode": mode,
            "order_size": status.get("order_size", 0.0),
            "order_notional": status.get("order_notional", 0.0),
            "position_size": engine.paper.position_size,
            "position_notional": position_notional,
            "cash": engine.paper.cash,
            "realized_pnl": engine.paper.realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "fees_paid": engine.paper.fees_paid,
            "equity": equity,
            "daily_pnl_pct": daily_pnl_pct,
            "risk_state": risk_state,
            "strategy_status": strategy_status,
            "pause_reason": reason,
            "last_error": "",
            "telegram_status": telegram_status,
        }

        last_trade = _read_last_valid_trade(TRADES_CSV, cfg.default_symbol)
        _write_last_100_real_trades(TRADES_CSV, LAST_100_REAL_TRADES_CSV, cfg.default_symbol)
        blocked_1h, blocked_by_reason_1h, last_block_reason = _risk_block_stats_window(now_ts=time.time())
        trade_stats_1h = _trade_stats_window(now_ts=time.time(), expected_symbol=cfg.default_symbol)
        position_side = "FLAT"
        if engine.paper.position_size > 0:
            position_side = "LONG"
        elif engine.paper.position_size < 0:
            position_side = "SHORT"
        status_payload.update(
            {
                "position_side": position_side,
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
                "allowed_to_reduce": bool(status.get("allowed_to_reduce", abs(engine.paper.position_size) > 1e-12)),
                "position_management_action": status.get("position_management_action", "none"),
                "total_pnl": engine.paper.realized_pnl + unrealized_pnl,
                "total_pnl_pct": ((engine.paper.realized_pnl + unrealized_pnl) / cfg.paper_start_balance_usd) if cfg.paper_start_balance_usd > 0 else None,
                "daily_pnl": equity - daily_start_equity,
                "daily_pnl_pct": daily_pnl_pct,
                "unrealized_pnl_pct": ((unrealized_pnl / abs(engine.paper.avg_entry * engine.paper.position_size)) if abs(engine.paper.avg_entry * engine.paper.position_size) > 1e-9 else 0.0),
                "exposure_pct": (position_notional / equity) if equity > 1e-9 else 0.0,
                "avg_entry": engine.paper.avg_entry,
                "trades_1h": trade_stats_1h["trades_count"],
                "buy_count_1h": trade_stats_1h["buy_count"],
                "sell_count_1h": trade_stats_1h["sell_count"],
                "volume_usd_1h": trade_stats_1h["volume_usd"],
                "fees_1h": trade_stats_1h["fees"],
                "realized_pnl_delta_1h": trade_stats_1h["realized_pnl_delta"],
                "trades_30m": trade_stats_1h["trades_count"],
                "buy_count_30m": trade_stats_1h["buy_count"],
                "sell_count_30m": trade_stats_1h["sell_count"],
                "volume_usd_30m": trade_stats_1h["volume_usd"],
                "fees_30m": trade_stats_1h["fees"],
                "realized_pnl_delta_30m": trade_stats_1h["realized_pnl_delta"],
                "no_fill_cycles": engine.no_fill_cycles,
            }
        )
        last_trade_ts = _resolve_last_real_trade_ts(cfg.default_symbol, fallback=(engine.last_real_trade_ts.isoformat() if engine.last_real_trade_ts else ""))
        last_trade_dt = _parse_iso_utc(str(last_trade_ts))
        last_trade_age_hours = ((datetime.now(timezone.utc) - last_trade_dt).total_seconds() / 3600) if last_trade_dt else None
        mark_price = latest_close
        buys = sorted([o for o in engine.open_orders if str(o.get("symbol", "")).upper() == cfg.default_symbol.upper() and str(o.get("side", "")).lower() == "buy"], key=lambda x: float(x.get("price", 0.0)), reverse=True)
        sells = sorted([o for o in engine.open_orders if str(o.get("symbol", "")).upper() == cfg.default_symbol.upper() and str(o.get("side", "")).lower() == "sell"], key=lambda x: float(x.get("price", 0.0)))
        if not buys and not sells:
            logger.info("no_grid_orders_generated")
        next_order = None
        if buys:
            next_order = {"side": "buy", "price": float(buys[0]["price"])}
        if sells and (next_order is None or abs(float(sells[0]["price"]) - mark_price) < abs(next_order["price"] - mark_price)):
            next_order = {"side": "sell", "price": float(sells[0]["price"])}
        next_exit_side = None
        next_exit_price = None
        if engine.paper.position_size > 0 and sells:
            next_exit_side = "sell"
            next_exit_price = float(sells[0]["price"])
        elif engine.paper.position_size < 0 and buys:
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
            tg.send_status_report({**status_payload, "status": status.get("status", "unknown"), "network": cfg.hl_network, "mode_name": "PAPER" if cfg.paper_mode else "LIVE", "grid_mode": mode, "fill_model": cfg.fill_model, "risk_reason": reason or "none", "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")})
            last_telegram_report_ts = now_ts

        engine.paper.current_day = current_day.isoformat()
        engine.paper.daily_start_equity = daily_start_equity
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
