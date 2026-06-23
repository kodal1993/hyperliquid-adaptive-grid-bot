# Trade-history loss analysis (2026-05-28 → 2026-06-23)

Reconstructed from a Hyperliquid exchange export (676 fills, BTC perp). FIFO
round-trip reconstruction reconciles to the exchange net (−$6.07 BTC vs −$6.07
reconstructed), so the breakdown below is trustworthy.

## Headline

| Metric | Value |
|---|---|
| Net realized PnL | **−$5.97** |
| Gross PnL (pre-fee) | −$0.65 (≈ flat) |
| Fees paid | **−$5.32** |
| Traded volume | $17,188 |
| Win rate | 54.5% (224W / 187L) |
| Avg winner / avg loser | +$0.035 / −$0.074 |
| Profit factor | 0.56 |
| Breakeven win rate | 68% (vs 54.5% actual) |

Gross was ~flat; **fees are what sank it**, and the fees are concentrated in a
small number of taker exits.

## Where the money went

1. **Jun 11 + Jun 14 size burst — −$3.57 (60% of the loss).** 48 fills at
   ~$150–160 notional (≈15× the normal $11–30), **all taker**, sub-minute holds,
   100% losers. This is an aggressive/non-grid churn episode under an older
   build. **Already structurally prevented:** `ExecutionEngine._submit_live_limit`
   now caps every order to `MAX_NOTIONAL_PER_TRADE_USD` ($24) and blocks any new
   order that would push the position past `MAX_POSITION_NOTIONAL_USD` ($90), so
   no code path (grid, flip, flatten) can resize to $160 again.

2. **~25 taker defensive exits — essentially the entire remaining loss.** The
   −$0.45…−$0.54 single-trade losers are wrong-way / adverse-move / stop exits
   that ran to 1.5% and then **crossed as taker**. Of the $5.32 fees, $2.26 was
   taker fee on exit fills; **~$1.51 of that was pure fee a maker exit would have
   avoided.**

3. **Directional bleed: longs −$5.05 vs shorts −$1.01.** BTC drifted ~73k→63k;
   the grid kept buying dips into a downtrend.

## Fixes applied in this change (mechanism-level, low-risk)

**Maker-first exits now re-peg instead of escalating to taker.** Previously the
forced-exit maker order was placed once at the passive touch and, if the market
walked away during the 8s window (exactly what happens on the trend moves that
trigger these exits), it escalated to a taker cross. Now, while the maker window
is open, the resting reduce-only order is re-pegged to the touch as the mark
drifts, so it keeps filling at the maker fee. The taker cross remains the
backstop once the (now longer) window expires.

- `src/execution_engine.py`: `_maybe_repeg_flatten` + `flatten_maker_repeg_pct`.
- `config/live.env`: `FLATTEN_MAKER_WAIT_SECONDS` 8 → 20,
  `FLATTEN_MAKER_REPEG_PCT=0.0006` (re-peg on ≥0.06% drift).
- Default `flatten_maker_repeg_pct=0.0` preserves the old behaviour when unset.

Grounded target: recover most of the **$1.51** taker-exit fee, and exit at the
ask rather than crossing the bid (better fill price too).

## Recommended levers NOT auto-applied (need live A/B + operator risk choice)

These change strategy behaviour, so validate one at a time on testnet/small size.

1. **Payoff ratio (the core expectancy problem).** TP is 1× spacing (~0.35%) but
   losers run to 1.5% — a ~4:1 adverse risk/reward, which is why a 54.5% win rate
   still loses. Options (do **not** combine 1a+1b at once):
   - **1a. Widen TP:** `PAIRED_TP_SPACING_MULTIPLIER` 1.0 → ~1.25. Bigger winners;
     trade-off is lower TP fill rate / longer holds.
   - **1b. Tighten stops — only now that exits fill maker:** `ADVERSE_MOVE_EXIT_PCT`
     / `WRONG_WAY_EXIT_LOSS_PCT` 0.015 → 0.010. ⚠️ History shows tight *taker*
     stops lost money in mean-reversion; revisit only on top of the maker re-peg
     above, and watch closely.
2. **Cap the dollar tail:** `MAX_POSITION_NOTIONAL_USD` 90 → 60. A 1.5% adverse
   move on a maxed same-side stack becomes ~$0.90 instead of ~$1.35. Pure
   variance reduction; expectancy-neutral.
3. **Counter-trend long suppression:** longs caused the directional bleed. Raise
   `COUNTER_TREND_ENTRY_EDGE_SCORE` (3.5→) and/or `STRONG_MOMENTUM_BLOCK_THRESHOLD`,
   and consider `MARKET_STRESS_EXTREME_FLAT=true`, so the grid stops buying dips
   in a confirmed downtrend.
