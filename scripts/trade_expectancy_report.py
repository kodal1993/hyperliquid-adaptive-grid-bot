#!/usr/bin/env python3
"""Generate trade expectancy diagnostics from the bot fill ledger.

This is a read-only reporting utility. It reconstructs closed position lots from
``logs/trades.jsonl`` (or a CSV ledger) and prints rolling expectancy windows so
live trading behavior is not changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Sequence

DEFAULT_LEDGER = Path("logs/trades.jsonl")
DEFAULT_WINDOWS = (100, 200, 300)
EPSILON = 1e-12
DUST_NOTIONAL_USD = 2.0
MIN_NOTIONAL_USD = 10.0


@dataclass
class OpenLot:
    signed_qty: float
    timestamp: datetime | None
    entry_fee_remaining: float
    price: float


@dataclass
class ClosedTrade:
    timestamp: datetime | None
    symbol: str
    side: str
    qty: float
    price: float
    notional: float
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    hold_seconds: float | None
    regime: str
    orderbook_classification: str
    prediction_bias: str
    fill_liquidity: str
    source_side: str

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
            raw = float(text)
            # Hyperliquid timestamps are usually epoch milliseconds.
            parsed = datetime.fromtimestamp(raw / 1000 if raw > 10_000_000_000 else raw, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def signed_qty(row: dict[str, Any]) -> float:
    qty = abs(parse_float(row.get("qty", row.get("size", row.get("sz")))))
    side = str(row.get("side", "")).lower()
    if side in {"buy", "b", "bid"}:
        return qty
    if side in {"sell", "s", "ask", "a"}:
        return -qty
    return 0.0


def row_side(row: dict[str, Any]) -> str:
    side = str(row.get("side", "")).lower()
    if side in {"buy", "b", "bid"}:
        return "buy"
    if side in {"sell", "s", "ask", "a"}:
        return "sell"
    return side or "unknown"


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"trade ledger not found: {path}")
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        with path.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    return sorted(rows, key=lambda row: parse_timestamp(row.get("timestamp") or row.get("time")) or datetime.min.replace(tzinfo=timezone.utc))


def _field(row: dict[str, Any], *names: str, default: str = "unknown") -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def _closed_trade_from_row(
    row: dict[str, Any],
    *,
    ts: datetime | None,
    qty: float,
    gross_pnl: float,
    entry_fee: float,
    exit_fee: float,
    side: str,
    hold_seconds: float | None,
) -> ClosedTrade:
    price = parse_float(row.get("price", row.get("px")))
    notional = abs(parse_float(row.get("notional"))) or abs(price * qty)
    return ClosedTrade(
        timestamp=ts,
        symbol=_field(row, "symbol", "coin", default=""),
        side=side,
        qty=qty,
        price=price,
        notional=notional,
        gross_pnl=gross_pnl,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        hold_seconds=hold_seconds,
        regime=_field(row, "regime"),
        orderbook_classification=_field(row, "orderbook_classification", "orderbook", "orderbook_state"),
        prediction_bias=_field(row, "prediction_bias", "prediction_direction", "bias"),
        fill_liquidity=_field(row, "fill_liquidity", "liquidity"),
        source_side=row_side(row),
    )


def reconstruct_closed_trades(rows: Iterable[dict[str, Any]]) -> list[ClosedTrade]:
    """FIFO-match fills into closed lots while preserving close-row diagnostics.

    If the ledger begins after a position was already open, a realized-PnL row
    cannot be matched to an entry lot. In that case the function still emits a
    best-effort closed trade using the fill side (sell closes a long, buy closes
    a short) so the expectancy report does not silently drop live PnL.
    """

    open_lots: deque[OpenLot] = deque()
    closed_trades: list[ClosedTrade] = []

    for row in rows:
        ts = parse_timestamp(row.get("timestamp") or row.get("time"))
        sqty = signed_qty(row)
        fill_qty = abs(sqty)
        if fill_qty <= EPSILON:
            continue

        realized = parse_float(row.get("realized_pnl_delta", row.get("closedPnl", row.get("closed_pnl"))))
        fee = abs(parse_float(row.get("fee")))
        price = parse_float(row.get("price", row.get("px")))
        remaining_signed = sqty
        remaining_fill_qty = fill_qty
        is_closing = bool(open_lots and open_lots[0].signed_qty * sqty < 0)
        close_qty_total = min(sum(abs(lot.signed_qty) for lot in open_lots), remaining_fill_qty) if is_closing else 0.0
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
                _closed_trade_from_row(
                    row,
                    ts=ts,
                    qty=close_qty,
                    gross_pnl=gross_part,
                    entry_fee=entry_fee_part,
                    exit_fee=exit_fee_part,
                    side="long" if lot.signed_qty > 0 else "short",
                    hold_seconds=hold_seconds,
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
            unmatched_realized = realized if abs(realized) > EPSILON and close_qty_total <= EPSILON else 0.0
            if abs(unmatched_realized) > EPSILON:
                # Best-effort close for ledgers that start mid-position.
                fallback_side = "short" if sqty > 0 else "long"
                closed_trades.append(
                    _closed_trade_from_row(
                        row,
                        ts=ts,
                        qty=remaining_fill_qty,
                        gross_pnl=unmatched_realized,
                        entry_fee=0.0,
                        exit_fee=fee * (remaining_fill_qty / fill_qty),
                        side=fallback_side,
                        hold_seconds=None,
                    )
                )
            else:
                open_fee = fee * (remaining_fill_qty / fill_qty)
                open_lots.append(OpenLot(signed_qty=remaining_signed, timestamp=ts, entry_fee_remaining=open_fee, price=price))

    return closed_trades


def avg(values: Sequence[float]) -> float | None:
    return mean(values) if values else None


def med(values: Sequence[float]) -> float | None:
    return median(values) if values else None


def profit_factor(values: Sequence[float]) -> float | None:
    wins = sum(v for v in values if v > EPSILON)
    losses = abs(sum(v for v in values if v < -EPSILON))
    if losses <= EPSILON:
        return None if wins <= EPSILON else math.inf
    return wins / losses


def fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.4f}"


def fmt_ratio(value: float | None) -> str:
    if value is None:
        return "n/a"
    if math.isinf(value):
        return "inf"
    return f"{value:.3f}"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2%}"


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


def summarize(trades: Sequence[ClosedTrade], total_source_fills: int) -> dict[str, Any]:
    net = [t.net_pnl for t in trades]
    gross = [t.gross_pnl for t in trades]
    fees = [t.total_fee for t in trades]
    winners = [t.net_pnl for t in trades if t.is_winner]
    losers = [t.net_pnl for t in trades if t.is_loser]
    hold_times = [t.hold_seconds for t in trades if t.hold_seconds is not None]
    winner_holds = [t.hold_seconds for t in trades if t.is_winner and t.hold_seconds is not None]
    loser_holds = [t.hold_seconds for t in trades if t.is_loser and t.hold_seconds is not None]
    gross_profit = sum(v for v in gross if v > EPSILON)
    gross_loss = abs(sum(v for v in gross if v < -EPSILON))
    net_profit = sum(v for v in net if v > EPSILON)
    avg_win = avg(winners)
    avg_loss = avg(losers)
    return {
        "total_trades": len(trades),
        "source_fill_count": total_source_fills,
        "closed_trade_count": len(trades),
        "win_rate": len(winners) / len(trades) if trades else None,
        "average_winner": avg_win,
        "average_loser": avg_loss,
        "median_winner": med(winners),
        "median_loser": med(losers),
        "largest_win": max(winners) if winners else None,
        "largest_loss": min(losers) if losers else None,
        "profit_factor_gross": (gross_profit / gross_loss) if gross_loss > EPSILON else (math.inf if gross_profit > EPSILON else None),
        "profit_factor_after_fees": profit_factor(net),
        "total_fees": sum(fees),
        "gross_pnl_before_fees": sum(gross),
        "net_pnl_after_fees": sum(net),
        "expectancy_per_trade": mean(net) if net else None,
        "average_fee_per_trade": mean(fees) if fees else None,
        "fee_gross_profit_ratio": (sum(fees) / gross_profit) if gross_profit > EPSILON else None,
        "long_only_pnl": sum(t.net_pnl for t in trades if t.side == "long"),
        "short_only_pnl": sum(t.net_pnl for t in trades if t.side == "short"),
        "average_hold_time": avg(hold_times),
        "winner_hold_time": avg(winner_holds),
        "loser_hold_time": avg(loser_holds),
        "dust_trade_count_under_2_usd": sum(1 for t in trades if 0.0 < t.notional < DUST_NOTIONAL_USD),
        "below_min_notional_fill_count_under_10_usd": sum(1 for t in trades if 0.0 < t.notional < MIN_NOTIONAL_USD),
        "win_loss_ratio": (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss is not None and abs(avg_loss) > EPSILON else None,
    }


def group_summary(trades: Sequence[ClosedTrade], key_fn) -> list[tuple[str, dict[str, Any]]]:
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        groups[str(key_fn(trade) or "unknown")].append(trade)
    return sorted(((key, summarize(items, len(items))) for key, items in groups.items()), key=lambda item: item[1]["net_pnl_after_fees"])


def render_metric_block(metrics: dict[str, Any]) -> list[str]:
    return [
        f"- Total trades: {metrics['total_trades']}",
        f"- Closed trade count: {metrics['closed_trade_count']}",
        f"- Win rate: {fmt_pct(metrics['win_rate'])}",
        f"- Average winner: {fmt_money(metrics['average_winner'])}",
        f"- Average loser: {fmt_money(metrics['average_loser'])}",
        f"- Median winner: {fmt_money(metrics['median_winner'])}",
        f"- Median loser: {fmt_money(metrics['median_loser'])}",
        f"- Largest win: {fmt_money(metrics['largest_win'])}",
        f"- Largest loss: {fmt_money(metrics['largest_loss'])}",
        f"- Profit factor gross: {fmt_ratio(metrics['profit_factor_gross'])}",
        f"- Profit factor after fees: {fmt_ratio(metrics['profit_factor_after_fees'])}",
        f"- Total fees: {fmt_money(metrics['total_fees'])}",
        f"- Gross PnL before fees: {fmt_money(metrics['gross_pnl_before_fees'])}",
        f"- Net PnL after fees: {fmt_money(metrics['net_pnl_after_fees'])}",
        f"- Expectancy per trade: {fmt_money(metrics['expectancy_per_trade'])}",
        f"- Average fee per trade: {fmt_money(metrics['average_fee_per_trade'])}",
        f"- Fee / gross profit ratio: {fmt_pct(metrics['fee_gross_profit_ratio'])}",
        f"- Long-only PnL: {fmt_money(metrics['long_only_pnl'])}",
        f"- Short-only PnL: {fmt_money(metrics['short_only_pnl'])}",
        f"- Average hold time: {fmt_duration(metrics['average_hold_time'])}",
        f"- Winner hold time: {fmt_duration(metrics['winner_hold_time'])}",
        f"- Loser hold time: {fmt_duration(metrics['loser_hold_time'])}",
        f"- Dust trade count under $2: {metrics['dust_trade_count_under_2_usd']}",
        f"- Below-min-notional fill count under $10: {metrics['below_min_notional_fill_count_under_10_usd']}",
    ]


def render_group(name: str, groups: list[tuple[str, dict[str, Any]]]) -> list[str]:
    lines = [f"### By {name}"]
    if not groups:
        return lines + ["- n/a"]
    lines.append("| Bucket | Trades | Win rate | Avg win | Avg loss | PF after fees | Fees | Gross PnL | Net PnL | Expectancy | Dust | < $10 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for key, m in groups:
        lines.append(
            f"| {key} | {m['closed_trade_count']} | {fmt_pct(m['win_rate'])} | {fmt_money(m['average_winner'])} | "
            f"{fmt_money(m['average_loser'])} | {fmt_ratio(m['profit_factor_after_fees'])} | {fmt_money(m['total_fees'])} | "
            f"{fmt_money(m['gross_pnl_before_fees'])} | {fmt_money(m['net_pnl_after_fees'])} | "
            f"{fmt_money(m['expectancy_per_trade'])} | {m['dust_trade_count_under_2_usd']} | "
            f"{m['below_min_notional_fill_count_under_10_usd']} |"
        )
    return lines


def recommendation(metrics: dict[str, Any], side_groups: list[tuple[str, dict[str, Any]]]) -> list[str]:
    avg_win = metrics["average_winner"]
    avg_loss = metrics["average_loser"]
    avg_loss_abs = abs(avg_loss or 0.0)
    fee = metrics["average_fee_per_trade"] or 0.0
    pf_after = metrics["profit_factor_after_fees"]
    gross_exp = (metrics["gross_pnl_before_fees"] / metrics["closed_trade_count"]) if metrics["closed_trade_count"] else None
    net_exp = metrics["expectancy_per_trade"]
    fee_ratio = metrics["fee_gross_profit_ratio"]
    long_pnl = metrics["long_only_pnl"]
    short_pnl = metrics["short_only_pnl"]
    worst_side = min(side_groups, key=lambda item: item[1]["net_pnl_after_fees"])[0] if side_groups else "unknown"

    winners_too_small = avg_win is None or (fee > 0 and avg_win <= fee * 2) or (avg_win is not None and avg_loss_abs > 0 and avg_win / avg_loss_abs < 1.0)
    losers_too_large = avg_win is not None and avg_loss_abs > avg_win * 1.25
    fees_too_high = (gross_exp is not None and net_exp is not None and gross_exp > 0 > net_exp) or (fee_ratio is not None and fee_ratio > 0.30)

    if losers_too_large:
        first_tune = "loss control: reduce adverse-move tolerance / stale-position time and retest closed-trade loss size."
    elif fees_too_high or winners_too_small:
        first_tune = "edge size: raise minimum expected net edge/fee multiple and grid spacing so average winners clear fees."
    elif pf_after is not None and pf_after < 1.0:
        first_tune = "entry filtering: tighten the weakest regime/orderbook/prediction bucket before changing position sizing."
    else:
        first_tune = "bucket filtering: keep trading logic unchanged and disable/tighten only the worst diagnostic bucket after review."

    return [
        "## Recommendation",
        f"- Are winners too small? {'Yes' if winners_too_small else 'No'} — average win {fmt_money(avg_win)} vs average loss magnitude {fmt_money(avg_loss_abs)} and average fee {fmt_money(fee)}.",
        f"- Are losers too large? {'Yes' if losers_too_large else 'No'} — loss/win magnitude ratio is {fmt_ratio((avg_loss_abs / avg_win) if avg_win else None)}.",
        f"- Are fees too high relative to edge? {'Yes' if fees_too_high else 'No'} — fee/gross-profit ratio is {fmt_pct(fee_ratio)}; gross expectancy is {fmt_money(gross_exp)} and net expectancy is {fmt_money(net_exp)}.",
        f"- Which side performs worse? {worst_side} — long-only PnL {fmt_money(long_pnl)}, short-only PnL {fmt_money(short_pnl)}.",
        f"- What should be tuned first? {first_tune}",
    ]


def telegram_section(metrics: dict[str, Any]) -> list[str]:
    fee_drag = metrics["average_fee_per_trade"]
    return [
        "## Telegram/performance report section",
        "```text",
        "EXPECTANCY",
        f"Avg win: {fmt_money(metrics['average_winner'])}",
        f"Avg loss: {fmt_money(metrics['average_loser'])}",
        f"Win/loss ratio: {fmt_ratio(metrics['win_loss_ratio'])}",
        f"PF after fees: {fmt_ratio(metrics['profit_factor_after_fees'])}",
        f"Fee drag: {fmt_money(fee_drag)} per trade ({fmt_pct(metrics['fee_gross_profit_ratio'])} of gross profit)",
        f"Dust count: {metrics['dust_trade_count_under_2_usd']}",
        "```",
    ]


def build_report(rows: Sequence[dict[str, Any]], trades: Sequence[ClosedTrade], windows: Sequence[int], include_all: bool) -> str:
    lines = [
        "# Trade Expectancy Diagnostic Report",
        "",
        f"Source fills: {len(rows)}",
        f"Reconstructed closed trades: {len(trades)}",
        "",
        "CLI command:",
        "```bash",
        "python -m scripts.trade_expectancy_report --limit 300",
        "```",
    ]

    if not trades:
        lines.extend([
            "",
            "No closed trades found. Closed-trade diagnostics require realized PnL rows in logs/trades.jsonl.",
        ])
        return "\n".join(lines)

    window_specs: list[tuple[str, Sequence[ClosedTrade]]] = [(f"last {w}", trades[-w:]) for w in windows]
    if include_all:
        window_specs.append(("all trades", trades))

    primary_metrics: dict[str, Any] | None = None
    primary_side_groups: list[tuple[str, dict[str, Any]]] | None = None
    for label, sample in window_specs:
        metrics = summarize(sample, len(rows))
        if primary_metrics is None:
            primary_metrics = metrics
        lines.extend(["", f"## Window: {label}", ""])
        lines.extend(render_metric_block(metrics))
        side_groups = group_summary(sample, lambda t: t.side)
        if primary_side_groups is None:
            primary_side_groups = side_groups
        lines.extend([""] + render_group("side", side_groups))
        lines.extend([""] + render_group("hour of day (UTC)", group_summary(sample, lambda t: f"{t.timestamp.hour:02d}:00" if t.timestamp else "unknown")))
        lines.extend([""] + render_group("regime", group_summary(sample, lambda t: t.regime)))
        lines.extend([""] + render_group("orderbook classification", group_summary(sample, lambda t: t.orderbook_classification)))
        lines.extend([""] + render_group("prediction bias", group_summary(sample, lambda t: t.prediction_bias)))

    assert primary_metrics is not None
    lines.extend([""] + telegram_section(primary_metrics))
    lines.extend([""] + recommendation(primary_metrics, primary_side_groups or []))
    return "\n".join(lines)


def parse_windows(raw: str) -> tuple[int, ...]:
    windows: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value <= 0:
            raise argparse.ArgumentTypeError("windows must be positive integers")
        windows.append(value)
    if not windows:
        raise argparse.ArgumentTypeError("at least one window is required")
    return tuple(windows)


def missing_ledger_report(path: Path, windows: Sequence[int], include_all: bool) -> str:
    labels = [f"last {w}" for w in windows] + (["all trades"] if include_all else [])
    return "\n".join([
        "# Trade Expectancy Diagnostic Report",
        "",
        f"Trade ledger not found: {path}",
        "No live trading behavior was changed. Provide --ledger pointing to logs/trades.jsonl from the running bot.",
        "",
        "Requested windows: " + ", ".join(labels),
        "",
        "CLI command:",
        "```bash",
        "python -m scripts.trade_expectancy_report --limit 300",
        "```",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Hyperliquid adaptive grid bot closed-trade expectancy.")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER, help="Path to trades.jsonl or trades.csv")
    parser.add_argument("--limit", type=int, default=None, help="Only print one recent closed-trade window, e.g. 300")
    parser.add_argument("--windows", type=parse_windows, default=DEFAULT_WINDOWS, help="Comma-separated recent closed-trade windows")
    parser.add_argument("--no-all", action="store_true", help="Do not include the all-trades window")
    parser.add_argument("--output", type=Path, help="Optional path to write the markdown report")
    args = parser.parse_args()

    windows = (args.limit,) if args.limit else args.windows
    if not args.ledger.exists():
        report = missing_ledger_report(args.ledger, windows, not args.no_all)
    else:
        rows = read_ledger(args.ledger)
        trades = reconstruct_closed_trades(rows)
        report = build_report(rows, trades, windows, not args.no_all)

    print(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
