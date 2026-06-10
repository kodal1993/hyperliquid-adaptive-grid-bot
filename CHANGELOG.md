# Changelog

## Address rate-limit cooldown

- When Hyperliquid rejects an order with the cumulative-request rate limit ("Too many cumulative requests sent ... Place taker orders to free up 1 request per USDC traded"), the live engine now enters a 120s cooldown during which new entry orders are skipped locally instead of being sent and rejected — every rejected attempt deepens the request deficit. Reduce-only closes stay allowed.
- During the cooldown the orchestrator also skips orphan/stale order cleanup: a one-sided grid is expected while placements are being rejected, and canceling the surviving side only burned the trickle budget on cancel/replace loops (observed live).

## Size-multiplier floor at exchange minimum notional

- Orderbook/prediction bias multipliers (e.g. 0.75x short-side reduction) could shrink a $12 grid order below Hyperliquid's $10 minimum notional, silently erasing the level instead of reducing it (observed live as repeated `min_notional_blocked` on the sell side). Multiplied sizes are now floored back to the configured minimum order notional while respecting the per-trade cap.

## Paired take-profit and WebSocket market data

- Added paired take-profit: every opening grid fill immediately places a reduce-only `Gtc` take-profit one grid spacing away (`PAIRED_TAKE_PROFIT_ENABLED=true`, `PAIRED_TP_SPACING_MULTIPLIER=1.0`), realizing grid roundtrips directly instead of waiting for a regime flip or recenter. Engines record the active plan spacing on every regrid for TP pricing; TP submission failures never break fill ingestion.
- Added a WebSocket market-data layer (`USE_WEBSOCKET=true`): the client subscribes to `l2Book`, `candle`, and `userFills` streams and serves `get_l2_book`, `get_candles`, and `get_user_fills` from in-memory caches, eliminating most per-tick REST polling. Every getter keeps an automatic REST fallback when the stream is unavailable or stale, candles re-seed from REST every 30 minutes, and user fills do a REST reconcile pass every 30th call.

## Execution efficiency: maker/taker fee model, post-only grid orders, diff-based regrid

- Split the flat `fee_rate` into `MAKER_FEE_RATE` (default 0.00015) and `TAKER_FEE_RATE` (default 0.00045); edge filters and the spacing fee floor now plan with the maker rate, since post-only grid orders always rest as maker. With default multipliers this lowers the spacing fee floor from ~0.22% to ~0.08% and stops profitable levels being filtered by a taker-fee assumption.
- Grid entry orders are now submitted post-only (`Alo` time-in-force, `USE_ALO_ORDERS=true` by default) so they can never cross the spread and pay taker fees; reduce-only closes keep `Gtc`. Post-only rejections are detected from the exchange response, logged as `live_order_alo_rejected`, and counted.
- Live order responses are now checked for embedded error statuses; rejected orders are logged (`live_order_rejected`) instead of being silently treated as placed.
- `cancel_replace_grid` is now diff-based in both live and paper engines: orders within `MIN_REPRICE_DISTANCE_PCT` of a desired level (and within 5% size tolerance) are kept instead of the previous cancel-all-and-replace, cutting order churn, API usage, and cancel/fill race windows. Live cancels are per-oid with post-cancel confirmation, and regrid no longer cancels unrelated reduce-only orders.
- Paper fill simulation charges the maker or taker rate based on fill liquidity metadata instead of a single flat rate.

## Live-only paper architecture cleanup

- Removed legacy paper deployment assets and paper profile entry points.
- Migrated production startup to always instantiate `LiveExecutionEngine` and log `execution_mode=live_only`.
- Introduced `ExecutionState` for live account, daily PnL baseline, fill de-duplication, ledger, status, dashboard, and Telegram reporting state.
- Retained `PaperExecutionEngine` only as an offline simulation utility for unit tests and historical analytics fixtures.
- Added live-only startup validation that rejects `PAPER_MODE=true`.

## Small-equity adaptive safety tuning

- Lowered missing-env defaults for small live equity: 3 grid levels, 10 USD order notional, 50 USD max position notional, 50 USD max active exposure, and 1800s stale order cleanup.
- Added ATR(14)/range-aware grid spacing with min floor, max cap, and structured logging for `spacing_source`, `atr_pct`, and `final_spacing_pct`.
- Added volatility-adjusted grid levels: low volatility 4 levels, normal 3, high 2, extreme 1 conservative level.
- Softened trend-biased grids to 70/30 by default, 80/20 only on strong confidence, and 60/40 on weak confidence.
- Added cleanup-aware `rebuild_reason` and one-cycle forced rebuilds after orphan/stale cleanup so recenter cooldowns do not leave the bot flat without a grid.
- Added a unified account/execution state source for strategy, risk, status, dashboard, and Telegram payloads, with `state_source=paper|live` logging.
- Kept daily PnL based on daily starting equity and persisted daily realized/fee baselines to avoid misleading percentage reports.

## Market Stress Engine Phase 1

- Added a BTC-only market stress decision layer based on local candle returns, candle range, ATR percentage, confirmed regime, and regime confidence.
- Integrated conservative market stress controls into live grid planning: trend-following one-sided grids in high/strong trends, no-new-exposure and reduce-only modes for uncertain high/extreme volatility, and opt-in emergency flattening.
- Added market stress telemetry to status payloads and Telegram `/status` reports.
- Added live env recommendations for approximately 160 USD equity. Emergency flat remains disabled by default via `MARKET_STRESS_EXTREME_FLAT=false`.
