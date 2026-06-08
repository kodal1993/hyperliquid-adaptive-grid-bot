from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any
import time


@dataclass(slots=True)
class OrderBookAnalysis:
    available: bool
    ready: bool
    stale: bool
    bid_volume_top10: float
    ask_volume_top10: float
    imbalance_ratio: float
    pressure_score: float
    classification: str
    long_size_multiplier: float
    short_size_multiplier: float
    orderbook_imbalance_ema: float | None
    samples: int
    decision: str
    fallback_reason: str = ""
    snapshot_age_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["orderbook_available"] = self.available
        payload["orderbook_stale"] = self.stale
        return payload


class OrderBookAnalyzer:
    def __init__(self, top_levels: int = 10, smoothing_window: int = 5, min_samples: int = 3, max_age_seconds: float = 5.0, epsilon: float = 1e-9) -> None:
        self.top_levels = max(int(top_levels), 1)
        self.smoothing_window = max(int(smoothing_window), 1)
        self.min_samples = max(int(min_samples), 1)
        self.max_age_seconds = max(float(max_age_seconds), 0.0)
        self.epsilon = max(float(epsilon), 1e-12)
        self._snapshots: deque[tuple[float, float]] = deque(maxlen=self.smoothing_window)
        self.orderbook_imbalance_ema: float | None = None

    def unavailable(self, decision: str = "orderbook_unavailable", *, fallback_reason: str | None = None, stale: bool = False, snapshot_age_seconds: float | None = None) -> OrderBookAnalysis:
        return OrderBookAnalysis(
            available=False,
            ready=False,
            stale=stale,
            bid_volume_top10=0.0,
            ask_volume_top10=0.0,
            imbalance_ratio=1.0,
            pressure_score=0.0,
            classification="Unavailable",
            long_size_multiplier=1.0,
            short_size_multiplier=1.0,
            orderbook_imbalance_ema=self.orderbook_imbalance_ema,
            samples=len(self._snapshots),
            decision=decision,
            fallback_reason=fallback_reason or decision,
            snapshot_age_seconds=snapshot_age_seconds,
        )

    def analyze(self, snapshot: Any) -> OrderBookAnalysis:
        if snapshot is None:
            return self.unavailable("orderbook_unavailable", fallback_reason="unavailable")
        age_seconds = self._snapshot_age_seconds(snapshot)
        if age_seconds is not None and self.max_age_seconds > 0 and age_seconds > self.max_age_seconds:
            return self.unavailable("orderbook_unavailable_stale", fallback_reason="stale", stale=True, snapshot_age_seconds=age_seconds)
        bids, asks = self._extract_levels(snapshot)
        if not bids or not asks:
            return self.unavailable("orderbook_unavailable_empty", fallback_reason="empty_or_malformed", snapshot_age_seconds=age_seconds)

        bid_volume = sum(size for _, size in bids[: self.top_levels])
        ask_volume = sum(size for _, size in asks[: self.top_levels])
        if bid_volume <= 0 and ask_volume <= 0:
            return self.unavailable("orderbook_unavailable_zero_volume", fallback_reason="empty", snapshot_age_seconds=age_seconds)

        raw_ratio = bid_volume / max(ask_volume, self.epsilon)
        self._snapshots.append((bid_volume, ask_volume))
        alpha = 2.0 / (self.smoothing_window + 1.0)
        if self.orderbook_imbalance_ema is None:
            self.orderbook_imbalance_ema = raw_ratio
        else:
            self.orderbook_imbalance_ema = alpha * raw_ratio + (1.0 - alpha) * self.orderbook_imbalance_ema

        avg_bid = sum(b for b, _ in self._snapshots) / len(self._snapshots)
        avg_ask = sum(a for _, a in self._snapshots) / len(self._snapshots)
        smoothed_ratio = self.orderbook_imbalance_ema if self.orderbook_imbalance_ema is not None else avg_bid / max(avg_ask, self.epsilon)
        pressure = (avg_bid - avg_ask) / max(avg_bid + avg_ask, self.epsilon)
        ready = len(self._snapshots) >= self.min_samples
        classification = self.classify(smoothed_ratio) if ready else "Warming Up"
        long_mult, short_mult = self.size_multipliers(classification if ready else "Neutral")
        return OrderBookAnalysis(
            available=True,
            ready=ready,
            stale=False,
            bid_volume_top10=avg_bid,
            ask_volume_top10=avg_ask,
            imbalance_ratio=smoothed_ratio,
            pressure_score=pressure,
            classification=classification,
            long_size_multiplier=long_mult,
            short_size_multiplier=short_mult,
            orderbook_imbalance_ema=self.orderbook_imbalance_ema,
            samples=len(self._snapshots),
            decision="orderbook_ready" if ready else "orderbook_warming_up",
            fallback_reason="" if ready else "warming_up",
            snapshot_age_seconds=age_seconds,
        )

    @staticmethod
    def classify(imbalance_ratio: float) -> str:
        if imbalance_ratio > 1.5:
            return "Strong Bullish"
        if imbalance_ratio >= 1.15:
            return "Bullish"
        if imbalance_ratio >= 0.85:
            return "Neutral"
        if imbalance_ratio >= 0.67:
            return "Bearish"
        return "Strong Bearish"

    @staticmethod
    def size_multipliers(classification: str) -> tuple[float, float]:
        if classification == "Strong Bullish":
            return 1.25, 0.75
        if classification == "Strong Bearish":
            return 0.75, 1.25
        return 1.0, 1.0

    def _extract_levels(self, snapshot: Any) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        if not isinstance(snapshot, dict):
            return [], []
        levels = snapshot.get("levels")
        if isinstance(levels, list) and len(levels) >= 2:
            return self._parse_side(levels[0]), self._parse_side(levels[1])
        bids = snapshot.get("bids") or snapshot.get("bid") or []
        asks = snapshot.get("asks") or snapshot.get("ask") or []
        return self._parse_side(bids), self._parse_side(asks)

    def _parse_side(self, levels: Any) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        if not isinstance(levels, list):
            return parsed
        for level in levels:
            try:
                if isinstance(level, dict):
                    price = float(level.get("px", level.get("price", 0.0)) or 0.0)
                    size = float(level.get("sz", level.get("size", level.get("volume", 0.0))) or 0.0)
                elif isinstance(level, (list, tuple)) and len(level) >= 2:
                    price = float(level[0] or 0.0)
                    size = float(level[1] or 0.0)
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if price > 0 and size > 0:
                parsed.append((price, size))
        return parsed

    def _snapshot_age_seconds(self, snapshot: Any) -> float | None:
        if not isinstance(snapshot, dict):
            return None
        for key in ("received_ts", "received_at", "timestamp", "time", "ts"):
            if key not in snapshot:
                continue
            raw = snapshot.get(key)
            try:
                if isinstance(raw, str) and any(ch in raw for ch in ("T", ":")):
                    observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    ts = observed.timestamp()
                else:
                    ts = float(raw)
            except (TypeError, ValueError):
                continue
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            return max(time.time() - ts, 0.0)
        return None
