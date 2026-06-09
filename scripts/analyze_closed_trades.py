#!/usr/bin/env python3
"""Diagnose expectancy from the recent closed-trade ledger.

The bot records fills in logs/trades.csv and logs/trades.jsonl. This utility
reconstructs closed lots from that fill ledger, takes the most recent closed
trades, and prints a diagnostics report without changing strategy behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


DEFAULT_LEDGER = Path("logs/trades.csv")
EPSILON = 1e-12


@dataclass
class OpenLot:
    signed_qty: float
    timestamp: datetime | None
    entry_fee_remaining: float


@dataclass
class ClosedTrade:
    timestamp: datetime | None
    symbol: str
    side: str
    qty: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    hold_seconds: float | None
    regime: str
    mode: str
    risk_state: str
    reason: str
    fill_liquidity: str
    orderbook_classification: str

    @property
    def total_fee(self) -> float:
        return self.entry_fee + self.exit_fee

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.total_fee

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > EPSILON

    @property
    def is_loser(self) -> bool:
        return self.net_pnl < -EPSILON


def parse_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromtimestamp(float(text) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def signed_qty(row: dict[str, Any]) -> float:
    qty = abs(parse_float(row.get("qty", row.get("size", row.get("sz")))))
    side = str(row.get("side", "")).lower()
    if side in {"buy", "b"}:
        return qty
    if side in {"sell", "s", "ask"}:
        return -qty
    return 0.0


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"trade ledger not found: {path}")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    return sorted(rows, key=lambda row: parse_timestamp(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))


def reconstruct_closed_trades(rows: Iterable[dict[str, Any]]) -> list[ClosedTrade]:
    open_lots: deque[OpenLot] = deque()
    closed_trades: list[ClosedTrade] = []

    for row in rows:
        ts = parse_timestamp(row.get("timestamp"))
        sqty = signed_qty(row)
        fill_qty = abs(sqty)
        if fill_qty <= EPSILON:
            continue

        realized = parse_float(row.get("realized_pnl_delta", row.get("closedPnl", row.get("closed_pnl"))))
        fee = abs(parse_float(row.get("fee")))
        remaining_signed = sqty
        remaining_fill_qty = fill_qty
        close_qty_total = min(abs(open_lots[0].signed_qty), remaining_fill_qty) if open_lots and open_lots[0].signed_qty * sqty < 0 else 0.0
        if open_lots and open_lots[0].signed_qty * sqty < 0:
            close_qty_total = min(sum(abs(lot.signed_qty) for lot in open_lots), remaining_fill_qty)
        close_fee_remaining = fee * (close_qty_total / fill_qty) if close_qty_total > EPSILON else 0.0

        while remaining_fill_qty > EPSILON and open_lots and open_lots[0].signed_qty * remaining_signed < 0:
            lot = open_lots[0]
            close_qty = min(abs(lot.signed_qty), remaining_fill_qty)
            close_fraction = close_qty / close_qty_total if close_qty_total > EPSILON else 0.0
            gross_part = realized * close_fraction if close_qty_total > EPSILON else 0.0
            entry_fee_part = lot.entry_fee_remaining * (close_qty / abs(lot.signed_qty)) if abs(lot.signed_qty) > EPSILON else 0.0
            exit_fee_part = close_fee_remaining * close_fraction if close_qty_total > EPSILON else 0.0
            hold_seconds = None
            if ts is not None and lot.timestamp is not None:
                hold_seconds = max((ts - lot.timestamp).total_seconds(), 0.0)

            closed_trades.append(
                ClosedTrade(
                    timestamp=ts,
                    symbol=str(row.get("symbol", row.get("coin", ""))),
                    side="long" if lot.signed_qty > 0 else "short",
                    qty=close_qty,
                    gross_pnl=gross_part,
                    entry_fee=entry_fee_part,
                    exit_fee=exit_fee_part,
                    hold_seconds=hold_seconds,
                    regime=str(row.get("regime", "")),
                    mode=str(row.get("mode", "")),
                    risk_state=str(row.get("risk_state", "")),
                    reason=str(row.get("reason", "")),
                    fill_liquidity=str(row.get("fill_liquidity", "")),
                    orderbook_classification=str(row.get("orderbook_classification", "")),
                )
            )

            lot_remainder = abs(lot.signed_qty) - close_qty
            if lot_remainder <= EPSILON:
                open_lots.popleft()
            else:
                lot.signed_qty = math.copysign(lot_remainder, lot.signed_qty)
                lot.entry_fee_remaining -= entry_fee_part
            remaining_signed += math.copysign(close_qty, -remaining_signed)
            remaining_fill_qty -= close_qty

        if remaining_fill_qty > EPSILON:
            open_fee = fee * (remaining_fill_qty / fill_qty)
            open_lots.append(OpenLot(signed_qty=remaining_signed, timestamp=ts, entry_fee_remaining=open_fee))

    return closed_trades


def profit_factor(values: list[float]) -> float | None:
    wins = sum(v for v in values if v > EPSILON)
    losses = abs(sum(v for v in values if v < -EPSILON))
    if losses <= EPSILON:
        return None if wins <= EPSILON else math.inf
    return wins / losses


def fmt_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.4f}"


def fmt_ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf"
    return f"{value:.3f}"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.2f} h"
    return f"{hours / 24:.2f} d"


def avg(values: list[float]) -> float | None:
    return mean(values) if values else None


def med(values: list[float]) -> float | None:
    return median(values) if values else None


def grouped_expectancy(trades: list[ClosedTrade], attr: str) -> list[tuple[str, int, float]]:
    groups: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        key = getattr(trade, attr) or "unknown"
        groups[key].append(trade.net_pnl)
    return sorted(((key, len(vals), mean(vals)) for key, vals in groups.items()), key=lambda item: item[2])


def build_report(trades: list[ClosedTrade], limit: int) -> str:
    recent = trades[-limit:]
    if not recent:
        return "No closed trades found. Closed trades require non-zero realized_pnl_delta/closedPnl rows in the ledger."

    net = [t.net_pnl for t in recent]
    gross = [t.gross_pnl for t in recent]
    winners = [t.net_pnl for t in recent if t.is_winner]
    losers = [t.net_pnl for t in recent if t.is_loser]
    hold_times = [t.hold_seconds for t in recent if t.hold_seconds is not None]
    winner_holds = [t.hold_seconds for t in recent if t.is_winner and t.hold_seconds is not None]
    loser_holds = [t.hold_seconds for t in recent if t.is_loser and t.hold_seconds is not None]
    win_rate = len(winners) / len(recent)
    loss_rate = len(losers) / len(recent)
    avg_winner = avg(winners)
    avg_loser = avg(losers)
    avg_loss_abs = abs(avg_loser or 0.0)
    gross_expectancy = mean(gross)
    net_expectancy = mean(net)
    fee_drag = mean([t.total_fee for t in recent])
    breakeven_wr = avg_loss_abs / ((avg_winner or 0.0) + avg_loss_abs) if avg_winner and avg_loss_abs > EPSILON else None

    cause_lines: list[str] = []
    if net_expectancy < 0:
        if gross_expectancy > 0 and fee_drag > abs(net_expectancy) * 0.5:
            cause_lines.append(f"Fees are the primary drag: gross expectancy is {fmt_money(gross_expectancy)}, fee drag averages {fmt_money(fee_drag)}, and net expectancy is {fmt_money(net_expectancy)}.")
        if breakeven_wr is not None and win_rate < breakeven_wr:
            cause_lines.append(f"Win rate is below breakeven for the observed payoff: {win_rate:.1%} actual vs {breakeven_wr:.1%} needed.")
        if avg_winner and avg_loss_abs > avg_winner:
            cause_lines.append(f"Average losers are {avg_loss_abs / avg_winner:.2f}x the size of average winners after fees.")
        if not cause_lines:
            cause_lines.append("Negative expectancy is broad-based across win rate, payoff, and fees rather than a single dominant bucket.")
    else:
        cause_lines.append(f"Net expectancy is positive at {fmt_money(net_expectancy)} per closed trade; no net-negative cause was found in this sample.")

    weakest_regimes = grouped_expectancy(recent, "regime")[:3]
    weakest_liquidity = grouped_expectancy(recent, "fill_liquidity")[:3]

    winners_too_small = avg_winner is None or (avg_winner <= fee_drag * 2) or (breakeven_wr is not None and breakeven_wr > 0.6)
    losers_too_large = bool(avg_winner and avg_loss_abs > avg_winner * 1.5)

    if losers_too_large:
        parameter = "Reduce ADVERSE_MOVE_EXIT_PCT by about 20-30% and re-test; this targets loss size directly without changing entry logic."
    elif winners_too_small or (gross_expectancy > net_expectancy and fee_drag > abs(net_expectancy)):
        parameter = "Increase MIN_EXPECTED_NET_PROFIT_FEE_MULTIPLE / MIN_NET_PROFIT_FEE_MULTIPLIER and widen GRID_SPACING_* minimums until average gross winner clears total fees by at least 2x."
    elif breakeven_wr is not None and win_rate < breakeven_wr:
        parameter = "Raise PREDICTION_CONFIDENCE_THRESHOLD or STRONG_MOMENTUM_BLOCK_THRESHOLD filtering strictness to remove the lowest-expectancy entries."
    else:
        parameter = "No single parameter dominates; segment by regime/orderbook bucket and disable or tighten the weakest bucket first."

    lines = [
        f"Closed-trade expectancy diagnostics (last {len(recent)} of {len(trades)} reconstructed closed trades)",
        "",
        "Metrics (net values include allocated entry and exit fees unless noted):",
        f"- Average winner: {fmt_money(avg_winner)}",
        f"- Average loser: {fmt_money(avg_loser)}",
        f"- Median winner: {fmt_money(med(winners))}",
        f"- Median loser: {fmt_money(med(losers))}",
        f"- Expectancy: {fmt_money(net_expectancy)} per closed trade",
        f"- Profit factor: {fmt_ratio(profit_factor(gross))} gross",
        f"- Profit factor after fees: {fmt_ratio(profit_factor(net))}",
        f"- Average hold time: {fmt_duration(avg(hold_times))}",
        f"- Average winner hold time: {fmt_duration(avg(winner_holds))}",
        f"- Average loser hold time: {fmt_duration(avg(loser_holds))}",
        "",
        "Context:",
        f"- Win rate: {win_rate:.1%} ({len(winners)} winners / {len(recent)} trades)",
        f"- Loss rate: {loss_rate:.1%} ({len(losers)} losers / {len(recent)} trades)",
        f"- Gross expectancy before fees: {fmt_money(gross_expectancy)}",
        f"- Average allocated fees per closed trade: {fmt_money(fee_drag)}",
        f"- Breakeven win rate at observed payoff: {breakeven_wr:.1%}" if breakeven_wr is not None else "- Breakeven win rate at observed payoff: n/a",
        "",
        "1. What causes net negative expectancy?",
        *(f"- {line}" for line in cause_lines),
        "",
        "2. Are winners too small?",
        f"- {'Yes' if winners_too_small else 'No'}. Average winner is {fmt_money(avg_winner)} versus average allocated fees of {fmt_money(fee_drag)} and average loser magnitude of {fmt_money(avg_loss_abs)}.",
        "",
        "3. Are losers too large?",
        f"- {'Yes' if losers_too_large else 'No'}. Average loser magnitude is {fmt_money(avg_loss_abs)}; this is {fmt_ratio((avg_loss_abs / avg_winner) if avg_winner else None)}x average winner size.",
        "",
        "4. What parameter change would improve expectancy most?",
        f"- {parameter}",
    ]

    if weakest_regimes:
        lines.extend(["", "Weakest regime buckets by net expectancy:"])
        lines.extend(f"- {name}: n={count}, expectancy={fmt_money(exp)}" for name, count, exp in weakest_regimes)
    if weakest_liquidity:
        lines.extend(["", "Weakest liquidity buckets by net expectancy:"])
        lines.extend(f"- {name}: n={count}, expectancy={fmt_money(exp)}" for name, count, exp in weakest_liquidity)

    counter = Counter(t.reason or "unknown" for t in recent)
    if counter:
        lines.extend(["", "Close/fill reasons:"])
        lines.extend(f"- {name}: {count}" for name, count in counter.most_common())

    return "\n".join(lines)


def missing_ledger_report(path: Path, limit: int) -> str:
    return "\n".join([
        f"Closed-trade expectancy diagnostics (last {limit} requested)",
        "",
        f"Trade ledger not found: {path}",
        "No closed trades could be analyzed in this workspace. Provide --ledger pointing to trades.csv or trades.jsonl from the running bot.",
        "",
        "Metrics:",
        "- Average winner: n/a",
        "- Average loser: n/a",
        "- Median winner: n/a",
        "- Median loser: n/a",
        "- Expectancy: n/a",
        "- Profit factor: n/a",
        "- Profit factor after fees: n/a",
        "- Average hold time: n/a",
        "- Average winner hold time: n/a",
        "- Average loser hold time: n/a",
        "",
        "1. What causes net negative expectancy?",
        "- Cannot determine without a trade ledger containing closed trades.",
        "",
        "2. Are winners too small?",
        "- Cannot determine without closed winner rows.",
        "",
        "3. Are losers too large?",
        "- Cannot determine without closed loser rows.",
        "",
        "4. What parameter change would improve expectancy most?",
        "- No parameter change is recommended from absent data; run this diagnostic against the production ledger first.",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze the most recent closed trades in a bot trade ledger.")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Path to trades.csv or trades.jsonl")
    parser.add_argument("--limit", type=int, default=300, help="Number of recent closed trades to analyze")
    parser.add_argument("--output", type=Path, help="Optional path to write the markdown diagnostics report")
    args = parser.parse_args()

    if not args.ledger.exists():
        report = missing_ledger_report(args.ledger, args.limit)
    else:
        rows = read_ledger(args.ledger)
        trades = reconstruct_closed_trades(rows)
        report = build_report(trades, args.limit)
    print(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
