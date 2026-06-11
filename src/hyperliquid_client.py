from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from collections.abc import Callable
from typing import Any, TypeVar

import requests
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

logger = logging.getLogger(__name__)

_RETRYABLE_HTTP_STATUSES = {500, 502, 503}
_DEFAULT_RETRY_DELAYS_SECONDS = (1, 2, 5, 10)
_R = TypeVar("_R")

_SIZE_DECIMALS_BY_SYMBOL = {"BTC": 5}
_DEFAULT_SIZE_DECIMALS = 8
_MAX_PRICE_SIGNIFICANT_FIGURES = 5
_MAX_PERP_PRICE_DECIMALS = 6


def _decimal_from_number(value: float | int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def normalize_price_decimal(symbol: str, price: float | int | str | Decimal) -> Decimal:
    """Return a Hyperliquid-safe limit price as Decimal.

    Hyperliquid rejects Python floats when the SDK would need to round them
    during float_to_wire conversion. Keeping the normalization in Decimal first
    lets all callers derive a short, deterministic decimal representation.
    """
    raw_price = _decimal_from_number(price)
    if raw_price <= 0:
        return Decimal("0")

    significant_figure_exponent = raw_price.adjusted() - _MAX_PRICE_SIGNIFICANT_FIGURES + 1
    decimal_exponent = max(significant_figure_exponent, -_MAX_PERP_PRICE_DECIMALS)
    quantum = Decimal(f"1e{decimal_exponent}")
    return raw_price.quantize(quantum, rounding=ROUND_HALF_UP).normalize()


def normalize_size_decimal(symbol: str, size: float | int | str | Decimal) -> Decimal:
    """Return a Hyperliquid-safe order size as Decimal, rounded down."""
    raw_size = _decimal_from_number(size)
    if raw_size <= 0:
        return Decimal("0")

    decimals = _SIZE_DECIMALS_BY_SYMBOL.get(symbol.upper(), _DEFAULT_SIZE_DECIMALS)
    quantum = Decimal(f"1e-{decimals}")
    return raw_size.quantize(quantum, rounding=ROUND_DOWN).normalize()


def normalize_price(symbol: str, price: float | int | str | Decimal) -> float:
    """Return a Hyperliquid-safe limit price using Decimal rounding."""
    return float(normalize_price_decimal(symbol, price))


def normalize_size(symbol: str, size: float | int | str | Decimal) -> float:
    """Return a Hyperliquid-safe order size rounded down for the symbol."""
    return float(normalize_size_decimal(symbol, size))


def _wire_float(value: Decimal) -> float:
    """Convert a normalized Decimal to a short float for the Hyperliquid SDK.

    The SDK currently accepts numeric floats, but it raises
    ``float_to_wire causes rounding`` when raw binary floats carry excess
    precision. Passing through a fixed decimal string first prevents accidental
    values such as 71059.67467196014 or 0.00014072480485628953 from reaching
    the SDK.
    """
    if value <= 0:
        return 0.0
    return float(format(value, "f"))


class HyperliquidClient:
    def __init__(
        self,
        private_key: str,
        account_address: str,
        network: str = "testnet",
        *,
        use_websocket: bool = False,
        ws_symbol: str = "",
        ws_candle_interval: str = "1m",
    ) -> None:
        self.private_key = private_key
        self.account_address = account_address
        self.network = network
        self.info: Any | None = None
        self.exchange: Any | None = None
        self.base_url = (
            "https://api.hyperliquid-testnet.xyz"
            if network == "testnet"
            else "https://api.hyperliquid.xyz"
        )
        self.retry_delays_seconds = _DEFAULT_RETRY_DELAYS_SECONDS
        self.consecutive_api_errors: int = 0
        self.use_websocket = bool(use_websocket)
        self.ws_symbol = str(ws_symbol or "").upper()
        self.ws_candle_interval = ws_candle_interval
        self.ws_active = False
        self.ws_orderbook_max_age_seconds = 10.0
        self.ws_candle_reseed_seconds = 1800.0
        self.ws_fills_reconcile_every = 30
        self._ws_lock = threading.Lock()
        self._ws_book: dict[str, Any] | None = None
        self._ws_book_ts = 0.0
        self._ws_candles: dict[int, dict[str, Any]] = {}
        self._ws_candle_ts = 0.0
        self._ws_candles_seed_ts = 0.0
        self._ws_fills: list[dict[str, Any]] = []
        self._ws_fill_ids: set[str] = set()
        self._ws_fills_snapshot = False
        self._ws_fill_calls = 0

    def connect(self) -> None:
        try:
            self.info = Info(self.base_url, skip_ws=not self.use_websocket)
            logger.info(
                "Hyperliquid SDK Info connected network=%s websocket=%s",
                self.network,
                self.use_websocket,
            )
        except Exception as exc:
            self.info = None
            logger.warning("SDK info init failed (websocket=%s): %s", self.use_websocket, exc)
            if self.use_websocket:
                try:
                    self.info = Info(self.base_url, skip_ws=True)
                    logger.warning("SDK info reconnected without websocket, falling back to REST polling")
                except Exception as rest_exc:
                    self.info = None
                    logger.warning("SDK info REST fallback init failed, using HTTP fallback: %s", rest_exc)
        self._start_ws_subscriptions()
        if self.private_key:
            try:
                wallet = Account.from_key(self.private_key)
                self.exchange = Exchange(
                    wallet,
                    base_url=self.base_url,
                    account_address=self.account_address or None,
                )
                logger.info(
                    "Hyperliquid SDK Exchange connected network=%s account_present=%s",
                    self.network,
                    bool(self.account_address),
                )
            except Exception as exc:
                self.exchange = None
                logger.error("Hyperliquid SDK Exchange init failed: %s", exc)

    # ------------------------------------------------------------------
    # WebSocket market-data layer. Every getter keeps a REST fallback, so a
    # dead or stale websocket degrades to the previous polling behavior
    # instead of stopping the bot.
    # ------------------------------------------------------------------

    def _start_ws_subscriptions(self) -> None:
        if not (self.use_websocket and self.info is not None and self.ws_symbol):
            return
        try:
            self.info.subscribe({"type": "l2Book", "coin": self.ws_symbol}, self._on_ws_l2_book)
            self.info.subscribe(
                {"type": "candle", "coin": self.ws_symbol, "interval": self.ws_candle_interval},
                self._on_ws_candle,
            )
            if self.account_address:
                self.info.subscribe(
                    {"type": "userFills", "user": self.account_address},
                    self._on_ws_user_fills,
                )
            self.ws_active = True
            logger.info(
                "websocket_subscriptions_active symbol=%s candle_interval=%s user_fills=%s",
                self.ws_symbol,
                self.ws_candle_interval,
                bool(self.account_address),
            )
        except Exception as exc:
            self.ws_active = False
            logger.warning("websocket_subscribe_failed fallback=rest_polling error=%s", exc)

    @staticmethod
    def _ws_payload(message: object) -> object:
        if isinstance(message, dict) and "data" in message:
            return message["data"]
        return message

    def _on_ws_l2_book(self, message: object) -> None:
        data = self._ws_payload(message)
        if not isinstance(data, dict) or "levels" not in data:
            return
        with self._ws_lock:
            self._ws_book = data
            self._ws_book_ts = time.time()

    def _on_ws_candle(self, message: object) -> None:
        data = self._ws_payload(message)
        candles = data if isinstance(data, list) else [data]
        now = time.time()
        with self._ws_lock:
            for candle in candles:
                if not isinstance(candle, dict) or candle.get("t") is None:
                    continue
                self._ws_candles[int(candle["t"])] = candle
                self._ws_candle_ts = now
            if len(self._ws_candles) > 800:
                for key in sorted(self._ws_candles)[:-600]:
                    del self._ws_candles[key]

    def _on_ws_user_fills(self, message: object) -> None:
        data = self._ws_payload(message)
        if not isinstance(data, dict):
            return
        fills = data.get("fills") or []
        with self._ws_lock:
            if data.get("isSnapshot"):
                self._ws_fills_snapshot = True
            for fill in fills:
                if isinstance(fill, dict):
                    self._add_ws_fill_locked(fill)

    @staticmethod
    def _ws_fill_key(fill: dict) -> str:
        return f"{fill.get('tid', fill.get('hash', ''))}:{fill.get('time', '')}"

    def _add_ws_fill_locked(self, fill: dict) -> None:
        key = self._ws_fill_key(fill)
        if key in self._ws_fill_ids:
            return
        self._ws_fill_ids.add(key)
        self._ws_fills.append(fill)
        if len(self._ws_fills) > 2000:
            self._ws_fills = self._ws_fills[-1500:]
            self._ws_fill_ids = {self._ws_fill_key(f) for f in self._ws_fills}

    def _ws_candles_usable(self, symbol: str, interval: str, lookback: int) -> bool:
        if not (self.ws_active and symbol.upper() == self.ws_symbol and interval == self.ws_candle_interval):
            return False
        now = time.time()
        with self._ws_lock:
            count = len(self._ws_candles)
            last_update = self._ws_candle_ts
            seed_ts = self._ws_candles_seed_ts
        if count < min(lookback, 30):
            return False
        if seed_ts and (now - seed_ts) > self.ws_candle_reseed_seconds:
            return False
        interval_seconds = self._interval_minutes(interval) * 60
        return (now - last_update) <= max(3 * interval_seconds, 90)

    def _is_retryable_exception(self, exc: BaseException) -> bool:
        if isinstance(exc, requests.exceptions.HTTPError):
            status_code = exc.response.status_code if exc.response is not None else None
            return status_code is not None and 500 <= status_code < 600
        return isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))

    def _with_retry(
        self,
        fn: Callable[..., _R],
        *args: Any,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        operation_name: str | None = None,
        **kwargs: Any,
    ) -> _R:
        """Exponential backoff retry. Raises the last exception after all attempts fail."""
        name = operation_name or getattr(fn, "__name__", "hyperliquid_api_call")
        for attempt in range(1, max_attempts + 1):
            try:
                result = fn(*args, **kwargs)
                self.consecutive_api_errors = 0
                return result
            except Exception as exc:
                if not self._is_retryable_exception(exc):
                    raise
                self.consecutive_api_errors += 1
                if self.consecutive_api_errors >= 5:
                    logger.critical(
                        "consecutive_hyperliquid_api_errors count=%s operation=%s",
                        self.consecutive_api_errors,
                        name,
                    )
                if attempt >= max_attempts:
                    raise
                delay_seconds = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "Retryable Hyperliquid API error during %s attempt=%s/%s retry_in_seconds=%s error=%s",
                    name,
                    attempt,
                    max_attempts,
                    delay_seconds,
                    exc,
                )
                time.sleep(delay_seconds)
        raise RuntimeError("unreachable Hyperliquid retry state")

    def _retry_hyperliquid_api(
        self, operation_name: str, operation: Callable[[], _R]
    ) -> _R:
        attempts = len(self.retry_delays_seconds) + 1
        for attempt in range(1, attempts + 1):
            try:
                result = operation()
                self.consecutive_api_errors = 0
                return result
            except Exception as exc:
                if not self._is_retryable_exception(exc):
                    raise
                self.consecutive_api_errors += 1
                if self.consecutive_api_errors >= 5:
                    logger.critical(
                        "consecutive_hyperliquid_api_errors count=%s operation=%s",
                        self.consecutive_api_errors,
                        operation_name,
                    )
                if attempt >= attempts:
                    raise
                delay_seconds = self.retry_delays_seconds[attempt - 1]
                logger.warning(
                    "Retryable Hyperliquid API error during %s attempt=%s/%s retry_in_seconds=%s error=%s",
                    operation_name,
                    attempt,
                    attempts,
                    delay_seconds,
                    exc,
                )
                time.sleep(delay_seconds)
        raise RuntimeError("unreachable Hyperliquid retry state")

    def _post_info(self, payload: dict[str, Any]) -> Any:
        def _request() -> Any:
            resp = requests.post(f"{self.base_url}/info", json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json()

        return self._retry_hyperliquid_api("POST /info", _request)

    def get_mid_price(self, symbol: str) -> float:
        if self.info is not None:
            mids = self._retry_hyperliquid_api("Info.all_mids", self.info.all_mids)
            if symbol in mids:
                return float(mids[symbol])
        mids = self._post_info({"type": "allMids"})
        return float(mids[symbol])

    def get_candles(
        self, symbol: str, interval: str = "1m", lookback: int = 200
    ) -> list[dict[str, Any]]:
        if self._ws_candles_usable(symbol, interval, lookback):
            with self._ws_lock:
                merged = [self._ws_candles[key] for key in sorted(self._ws_candles)]
            return merged[-lookback:]
        candles = self._get_candles_rest(symbol, interval, lookback)
        if (
            self.ws_active
            and symbol.upper() == self.ws_symbol
            and interval == self.ws_candle_interval
            and isinstance(candles, list)
        ):
            with self._ws_lock:
                for candle in candles:
                    if isinstance(candle, dict) and candle.get("t") is not None:
                        # setdefault: a fresher websocket update of the live candle
                        # must not be clobbered by the REST snapshot.
                        self._ws_candles.setdefault(int(candle["t"]), candle)
                self._ws_candles_seed_ts = time.time()
        return candles

    def _get_candles_rest(
        self, symbol: str, interval: str = "1m", lookback: int = 200
    ) -> list[dict[str, Any]]:
        interval_minutes = self._interval_minutes(interval)
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - lookback * interval_minutes * 60 * 1000
        if self.info is not None:
            try:
                return self._retry_hyperliquid_api(
                    "Info.candles_snapshot",
                    lambda: self.info.candles_snapshot(
                        symbol, interval, start_ms, end_ms
                    ),
                )
            except Exception:
                pass
        return self._post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )


    def get_l2_book(self, symbol: str) -> dict[str, Any]:
        if self.ws_active and symbol.upper() == self.ws_symbol:
            with self._ws_lock:
                book, book_ts = self._ws_book, self._ws_book_ts
            if book is not None and (time.time() - book_ts) <= self.ws_orderbook_max_age_seconds:
                return {**book, "received_ts": book_ts}
        if self.info is not None:
            try:
                return self._retry_hyperliquid_api(
                    "Info.l2_snapshot", lambda: self.info.l2_snapshot(symbol)
                )
            except Exception:
                pass
        return self._post_info({"type": "l2Book", "coin": symbol})

    def _interval_minutes(self, interval: str) -> int:
        if interval.endswith("m"):
            return max(1, int(interval[:-1]))
        if interval.endswith("h"):
            return max(1, int(interval[:-1]) * 60)
        if interval.endswith("d"):
            return max(1, int(interval[:-1]) * 24 * 60)
        return 1

    def has_live_execution_support(self) -> bool:
        """Return True when authenticated Hyperliquid order/cancel/fill APIs are usable."""
        if not self.private_key or not self.account_address:
            return False
        if self.exchange is not None and self.info is not None:
            return True
        self.connect()
        return self.exchange is not None and self.info is not None

    def require_live_execution_support(self) -> None:
        if not self.has_live_execution_support():
            raise RuntimeError(
                "live_execution_not_implemented: authenticated Hyperliquid Exchange client is unavailable"
            )

    def get_user_state(self) -> dict[str, Any]:
        if self.info is not None and self.account_address:
            return self._retry_hyperliquid_api(
                "Info.user_state", lambda: self.info.user_state(self.account_address)
            )
        if self.account_address:
            return self._post_info(
                {"type": "clearinghouseState", "user": self.account_address}
            )
        return {}


    def get_account_state(self) -> dict[str, Any]:
        return self.get_user_state()

    def get_account_summary(self) -> dict[str, Any]:
        state = self.get_user_state()
        margin = state.get("marginSummary", {}) if isinstance(state, dict) else {}
        withdrawable = (
            state.get("withdrawable", 0.0) if isinstance(state, dict) else 0.0
        )
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

    def get_position(self, symbol: str) -> dict[str, Any]:
        for item in self.get_positions():
            pos = item.get("position", item) if isinstance(item, dict) else {}
            if str(pos.get("coin", "")).upper() == symbol.upper():
                return pos
        return {}

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        if self.info is not None and self.account_address:
            orders = self._retry_hyperliquid_api(
                "Info.open_orders", lambda: self.info.open_orders(self.account_address)
            )
            return [o for o in orders if o.get("coin") == symbol]
        return []

    def get_user_rate_limit(self) -> dict[str, Any]:
        """Fetch the address-level request budget (cumVlm, nRequestsUsed, nRequestsCap)."""
        if not self.account_address:
            return {}
        data = self._post_info({"type": "userRateLimit", "user": self.account_address})
        return data if isinstance(data, dict) else {}

    def get_user_fills(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.ws_active and self._ws_fills_snapshot:
            self._ws_fill_calls += 1
            # Serve fills from the websocket stream; every Nth call still does a
            # REST reconcile pass in case the stream dropped messages.
            if self.ws_fills_reconcile_every <= 0 or self._ws_fill_calls % self.ws_fills_reconcile_every != 0:
                with self._ws_lock:
                    fills = list(self._ws_fills)
                if symbol:
                    return [f for f in fills if str(f.get("coin", "")).upper() == symbol.upper()]
                return fills
        if self.info is None or not self.account_address:
            return []
        fills = self._retry_hyperliquid_api(
            "Info.user_fills", lambda: self.info.user_fills(self.account_address)
        )
        if self.ws_active and isinstance(fills, list):
            with self._ws_lock:
                for fill in fills:
                    if isinstance(fill, dict):
                        self._add_ws_fill_locked(fill)
        if symbol:
            return [
                f for f in fills if str(f.get("coin", "")).upper() == symbol.upper()
            ]
        return fills

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        *,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> Any:
        self.require_live_execution_support()
        assert self.exchange is not None
        normalized_size_decimal = normalize_size_decimal(symbol, size)
        normalized_price_decimal = normalize_price_decimal(symbol, price)
        normalized_size = _wire_float(normalized_size_decimal)
        normalized_price = _wire_float(normalized_price_decimal)
        if normalized_size <= 0 or normalized_price <= 0:
            raise ValueError(
                f"invalid_normalized_order: symbol={symbol} side={side} raw_size={size} raw_price={price} normalized_size={normalized_size} normalized_price={normalized_price}"
            )
        # Alo (post-only) guarantees the order rests as maker; the exchange
        # rejects it instead of crossing the book, so it must never be used on
        # orders that need immediate execution (e.g. aggressive reduce-only closes).
        tif = "Alo" if post_only else "Gtc"
        logger.debug(
            "place_limit_order_normalized symbol=%s side=%s raw_size=%s raw_price=%s normalized_size=%s normalized_price=%s reduce_only=%s tif=%s",
            symbol,
            side,
            size,
            price,
            normalized_size,
            normalized_price,
            reduce_only,
            tif,
        )
        return self.exchange.order(
            symbol,
            side.lower() == "buy",
            normalized_size,
            normalized_price,
            {"limit": {"tif": tif}},
            reduce_only=reduce_only,
        )

    def cancel_order(self, symbol: str, oid: int) -> Any:
        self.require_live_execution_support()
        assert self.exchange is not None
        return self.exchange.cancel(symbol, int(oid))

    def cancel_all_orders(self, symbol: str) -> int:
        canceled = 0
        for order in self.get_open_orders(symbol):
            oid = order.get("oid")
            if oid is None:
                continue
            self.cancel_order(symbol, int(oid))
            canceled += 1
        return canceled

    def set_leverage(self, symbol: str, leverage: int, *, is_cross: bool = True) -> Any:
        self.require_live_execution_support()
        assert self.exchange is not None
        return self._retry_hyperliquid_api(
            "Exchange.update_leverage",
            lambda: self.exchange.update_leverage(
                int(leverage), symbol, is_cross=is_cross
            ),
        )
