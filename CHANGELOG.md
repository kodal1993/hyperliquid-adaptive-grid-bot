# Changelog

## Less grid chasing: wider regrid threshold and reprice distance

- Live order history showed the grid recentering and chasing price every ~90s during a slow drift (buy levels trailing a rising market without filling), burning request budget and producing ~6h of no fills in a 0.9% range. Raised `REGRID_THRESHOLD_PCT` 0.003 → 0.006 and `MIN_REPRICE_DISTANCE_PCT` 0.0015 → 0.003 so the grid holds its levels through larger moves, letting price oscillate into resting orders (more fills) and cutting cancel/replace churn (less budget burn). The regime detector and market-stress layer still handle genuine trends; the grid itself benefits from holding levels. Applied to defaults and both env templates.

## BTC + ETH on one account (parallel ETH service)

- Added support for running ETH alongside BTC on the **same Hyperliquid account** as a second, independent service — without touching the proven single-symbol BTC bot. Two processes on one address share the per-address request budget and coordinate through the budget poll (both see the same headroom and enter frugal/recovery together); the recovery hourly op budget is meant to be lowered to 3 per process so the combined consumption matches the single-symbol cap.
- `szDecimals` (order size rounding) is now seeded with known values (`BTC=5`, `ETH=4`) and refreshed from the exchange `meta` at `connect()`, so any traded symbol gets correct size steps instead of the BTC-hardcoded value — preventing the size-rounding rejection class of bug on ETH.
- Added `config/live-eth.env.example` (ETH risk caps: $20 order, $40 max position, 0.60 directional pct so the order clears the exposure cap at the smaller ceiling; separate state/lock files), `deploy/systemd/hyperliquid-grid-bot-eth.service`, and `docs/multi-symbol-eth.md` with the deploy steps. Risk split is BTC-weighted 60/40 (BTC $60 / ETH $40 max position, ~31% combined exposure at 1x).

## Scale order sizing for ~325 USD equity

- Raised the default order sizing now that equity is ~$325: `ORDER_NOTIONAL_USD` 12 → 20, `MAX_NOTIONAL_PER_TRADE_USD` → 24, `MAX_POSITION_NOTIONAL_USD` 40 → 90 (~28% of equity at 1x, liquidation negligible), `MAX_ACTIVE_EXPOSURE_USD` 50 → 110 so the full 3-level grid builds. `MIN_ORDER_NOTIONAL_USD` stays 12, above the $10 exchange minimum, so bias multipliers (0.75x) now reduce a $20 level to ~$15 instead of being floored away. Larger fills also repay the Hyperliquid request budget faster. Leverage stays 1x. Live deploys still need the same values in `config/live.env`, which overrides these defaults.

## Survive transient exchange errors instead of crashing

- A Hyperliquid-side `502 Bad Gateway` (their infra hiccup) crashed the whole bot: any unhandled exception in the tick propagated up, wrote `status=crashed`, and exited the process, leaving the grid and any open position unmanaged until a manual restart. The main loop now wraps each tick in a resilient handler: transient errors are logged, the status is written as `degraded` (not `crashed`), a throttled Telegram alert fires after 3 consecutive failures, and the bot keeps retrying with capped backoff (up to 120s) instead of dying. `KeyboardInterrupt` still stops it cleanly. A successful tick resets the failure counter.

## Stop discretionary cancel-all churn from bypassing the recovery budget

- Live logs showed the deficit still creeping (~45 requests/hour, frozen volume, headroom −227 → −339 over an afternoon) even with the hourly cap deployed. Root cause: the hourly budget only gated `cancel_replace_grid`, but the discretionary `cancel_all_orders` paths (market-stress one-siding when flat, `neutral_blocked_in_trend`, orphan cleanup) bypassed it entirely. In a trend they emptied the book every cycle; the empty-book re-seed immediately re-placed one order, and the loop burned ~2 requests per round with zero fills.
- `cancel_all_orders` now takes `force`. Genuine risk cancels (wrong-way exit, adverse-move exit, emergency flat, max-position reduce-only, daily-loss, hard-risk block) pass `force=True` and always execute. Discretionary cancels default to `force=False` and are skipped while in rate-limit recovery and over the hourly action budget, so a resting order survives instead of feeding the cancel/re-seed loop.
- The no-fill watchdog is also suppressed during recovery: tightening spacing to chase fills is pointless when the regrid is budget-frozen, and it was thrashing the resting order's target price (0.30% ↔ 0.24%).

## Hard hourly action budget while in deficit

- Daytime volatility passed the recovery reprice gates often enough that legitimate reshuffles still outspent the fill income (observed live: +16 requests in 30 minutes with zero volume, headroom −140 → −218 over a morning). While the address is in rate-limit recovery, grid maintenance now has a hard hourly exchange-action budget (`RATE_LIMIT_RECOVERY_MAX_OPS_PER_HOUR`, default 6): reshuffles beyond it are skipped with `rate_limit_hourly_op_budget`. An empty book may always be re-seeded, and reduce-only TPs are exempt because they only follow fills that just paid for themselves.

## Min-notional floor must round up to the size step

- Live logs showed the sell side never placing for hours: `live_order_skipped reason=min_notional side=sell qty=0.00015 notional=9.44`. The bias-multiplier floor computed the exact minimum size (10 / 62927 = 0.000158913), but the submit-side normalization rounds **down** to the symbol's size step, landing one step below the minimum again. `_floor_size_to_min_order_notional` now rounds up to the size step (`normalize_size_up`), overshooting the minimum by at most one step, so floored levels survive submission. This restores the sell half of the grid (and its fills/volume).

## No price-chasing while repaying the request deficit

- After the orphan fix the live burn dropped from ~27 to ~15 requests/hour, but the budget alerts showed it still bleeding with zero volume: the single resting order kept being canceled after the normal 90s lifetime and re-placed ~0.15% higher as the price trended away — 2 requests per chase, no fills. While in rate-limit recovery/frugal mode the reprice gates now harden to `RATE_LIMIT_RECOVERY_MIN_ORDER_LIFETIME_SECONDS` (600s) and `RATE_LIMIT_RECOVERY_MIN_REPRICE_DISTANCE_PCT` (0.5%, floored at one full grid spacing), so resting orders stay put and wait for a pullback instead of following the price.
- The anti-churn gates (order lifetime, reprice distance) now only guard cancels: when the diff has nothing to cancel and levels are missing, the regrid is allowed to fill them in (`missing_levels_fill_in`) regardless of how young the resting orders are.

## Flat-orphan cleanup no longer nukes a healthy one-sided grid

- Observed live (Order History): the bot placed a ~$12 buy, the flat-orphan cleanup canceled it within 14–16 seconds because the *sell* side was "missing", the forced rebuild re-placed the same buy (same price), and the loop repeated — zero fills, steady request burn, with ~10-minute quiet gaps wherever a trickle rejection re-armed the rate-limit orphan grace.
- The cleanup's expectation now follows the **last executed plan**: a side that stress one-siding or the edge/orderbook filters generated no levels for is not "missing". Orders younger than `MIN_ORDER_LIFETIME_SECONDS` never count as orphans (the op-budgeted regrid may simply not have placed the other side yet), and a confirmed missing side must persist for `ORPHAN_CLEANUP_CONFIRM_TICKS` (default 3) consecutive ticks before anything is canceled.

## Proactive budget watch, flip-churn brake, maker TPs, health telemetry

- **Proactive request-budget monitoring**: the live engine polls the public `userRateLimit` endpoint every `RATE_LIMIT_BUDGET_POLL_SECONDS` (600s) and exposes cumulative volume, requests used/cap, headroom and requests-per-USDC. Below `RATE_LIMIT_HEADROOM_ALERT` (1000) a Telegram alert fires (risk-alert cooldown applies); below `RATE_LIMIT_HEADROOM_FRUGAL` (500) the engine pre-arms the frugal recovery regrid *before* the exchange ever rejects a request. The June deficit was visible in these numbers weeks in advance.
- **Time-based trend-flip cooldown**: `TREND_FLIP_COOLDOWN_MINUTES` (10) now floors the tick-based cooldown (6 ticks @ 10s was only 60s), stopping the loss-making `Long > Short` / `Short > Long` flip churn observed live minutes apart on June 4–5.
- **Paired TPs prefer post-only**: take-profits are submitted as `Alo` first to keep the 0.015% maker rate, and only fall back to crossing `Gtc` when the price already moved past the level (`PAIRED_TP_POST_ONLY=true`). Live maker share had eroded from 100% to ~65–70% via taker closes at 3x the fee.
- **24h health telemetry**: status payload and the Telegram report now carry `trade_health_24h` (fills, maker share, net realized PnL, fees, avg roundtrip net, order ops per fill, ops per traded USDC) plus the polled `rate_limit_budget`, so degradation is visible from the phone instead of post-hoc CSV analysis.
- **Trickle action spacing**: while in rate-limit recovery/frugal mode, order operations keep `RATE_LIMIT_MIN_ACTION_INTERVAL_SECONDS` (12s) between exchange actions so a borderline-timed action cannot get rejected and re-arm the cooldown.

## Rate-limit recovery mode: placements before cancels

- Fixed the trickle-budget deadlock that left the bot with no resting orders (and therefore no fills) while the address carries a Hyperliquid cumulative-request deficit. In that state the exchange accepts roughly one action per 10 seconds, but the regrid spent its op budget cancel-first: the accepted slot went to a cancel, the replacement placement was rejected, and the rejection re-armed a 120–900s freeze — every cycle removed an order and placed none, so the grid bled to zero and no volume was ever traded to repay the deficit.
- For `RATE_LIMIT_RECOVERY_WINDOW_SECONDS` (default 1800s) after the last cumulative rate-limit rejection, the live regrid now runs in recovery mode: one order operation per cycle, spent on the placement closest to the grid center (the level most likely to fill — fills are the only thing that repays the deficit). Existing resting orders are kept as fill chances; a cancel only runs when there is nothing left to place or the book already holds the desired number of levels.
- Outside recovery mode the regrid now reserves at least half of `MAX_ORDER_OPS_PER_CYCLE` for placements whenever there is something to place, so a cancel backlog can no longer starve the grid of resting orders, and pending placements are always ordered closest-to-mid first.
- `last_rate_limit_ts` is persisted in the live state file so a restart does not drop the bot out of recovery mode.

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
