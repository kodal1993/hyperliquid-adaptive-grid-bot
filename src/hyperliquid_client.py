from __future__ import annotations

import logging
import time
from typing import Any
import requests

logger = logging.getLogger(__name__)


class HyperliquidClient:
    def __init__(self, private_key: str, account_address: str, network: str = "testnet") -> None:
        self.private_key = private_key
        self.account_address = account_address
        self.network = network
        self.info: Any | None = None
        self.base_url = "https://api.hyperliquid-testnet.xyz" if network == "testnet" else "https://api.hyperliquid.xyz"

    def connect(self) -> None:
        try:
            from hyperliquid.info import Info

            self.info = Info(self.base_url, skip_ws=True)
            logger.info("Hyperliquid SDK Info connected network=%s", self.network)
        except Exception as exc:
            self.info = None
            logger.warning("SDK init failed, using HTTP fallback: %s", exc)

    def _post_info(self, payload: dict[str, Any]) -> Any:
        resp = requests.post(f"{self.base_url}/info", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_mid_price(self, symbol: str) -> float:
        if self.info is not None:
            mids = self.info.all_mids()
            if symbol in mids:
                return float(mids[symbol])
        mids = self._post_info({"type": "allMids"})
        return float(mids[symbol])

    def get_candles(self, symbol: str, interval: str = "1m", lookback: int = 200) -> list[dict[str, Any]]:
        interval_minutes = self._interval_minutes(interval)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback * interval_minutes * 60 * 1000
        if self.info is not None:
            try:
                return self.info.candles_snapshot(symbol, interval, start_ms, end_ms)
            except Exception:
                pass
        return self._post_info({"type": "candleSnapshot", "req": {"coin": symbol, "interval": interval, "startTime": start_ms, "endTime": end_ms}})

    def _interval_minutes(self, interval: str) -> int:
        if interval.endswith("m"):
            return max(1, int(interval[:-1]))
        if interval.endswith("h"):
            return max(1, int(interval[:-1]) * 60)
        if interval.endswith("d"):
            return max(1, int(interval[:-1]) * 24 * 60)
        return 1


    def has_live_execution_support(self) -> bool:
        """Return True only when authenticated order/cancel/fill execution is implemented.

        The current client intentionally exposes read-only market/account helpers.
        Startup safety gates use this to prevent paper-simulated fills from running
        under a live trading configuration.
        """
        return False

    def get_user_state(self) -> dict[str, Any]:
        if self.info is not None and self.account_address:
            return self.info.user_state(self.account_address)
        if self.account_address:
            return self._post_info({"type": "clearinghouseState", "user": self.account_address})
        return {}

    def get_account_summary(self) -> dict[str, Any]:
        state = self.get_user_state()
        margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
        withdrawable = state.get("withdrawable", 0.0) if isinstance(state, dict) else 0.0
        positions = state.get("assetPositions", []) if isinstance(state, dict) else []
        return {
            "accountValue": float(margin.get("accountValue", 0.0) or 0.0),
            "withdrawable": float(withdrawable or 0.0),
            "assetPositions_count": len(positions or []),
        }

    def get_balance(self) -> dict[str, Any]:
        return {"equity": self.get_account_summary().get("accountValue", 0.0)}

    def get_positions(self) -> list[dict[str, Any]]:
        state = self.get_user_state()
        if isinstance(state, dict):
            return state.get("assetPositions", [])
        return []

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        if self.info is not None and self.account_address:
            orders = self.info.open_orders(self.account_address)
            return [o for o in orders if o.get("coin") == symbol]
        return []
