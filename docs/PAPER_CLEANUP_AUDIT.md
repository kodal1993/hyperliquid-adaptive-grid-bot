# Paper Trading Cleanup Audit

## Reference audit

Searched for:

- `paper`
- `paper_position`
- `paper_engine`
- `fill_model`
- `simulated_fill`
- `paper_balance`
- `paper_equity`
- `mode=paper`

## Categorization

### A) Still required

- `PaperExecutionEngine` / `ExecutionEngine` in `src/execution_engine.py`: retained as an offline simulation utility for unit tests and historical analytics fixtures only.
- Paper references in `tests/test_bot.py`: retained or migrated where they explicitly exercise offline fill simulation, historical ledger math, and regression coverage.
- `PAPER_MODE=false` guard text in live docs, README, config example, and preflight script: retained because startup validation must reject paper mode in production.

### B) Dead code removed

- `config/paper.env`
- `scripts/start_hyperliquid_paper.sh`
- `scripts/setup_dual_instance_layout.sh`
- `deploy/systemd/hyperliquid-grid-bot-paper.service`
- `docs/PAPER_LIVE_SPLIT.md`
- Production startup branch that instantiated the offline paper execution engine.
- Production stale-position cleanup path that called offline `_apply_fill()`.
- Telegram/reporting references that displayed paper mode or fill model in production status.

### C) Migration needed / completed

- Production state now uses `ExecutionState` on `LiveExecutionEngine` for daily PnL baselines, realized PnL, fees, live equity, live position, open orders, and fill de-duplication metadata.
- Strategy/risk orchestration reads account state from `execution_engine.account_state(...)`, which is live exchange sourced in production.
- Dashboard/status and Telegram payloads now use live state and `mode_name=LIVE` in production.
- Startup validation rejects `PAPER_MODE=true` with `live_only_mode_required`.

## Migration plan

1. Replace paper state with unified `ExecutionState`.
   - Completed for live runtime: `LiveExecutionEngine.state` persists daily baseline and fill sync metadata.
2. Replace paper position source with live exchange source.
   - Completed for live runtime: `LiveExecutionEngine.account_state()` calls `sync_account()` and reads balance/position/open orders from Hyperliquid.
3. Replace paper ledger with live ledger where applicable.
   - Completed for live runtime: ledger entries are emitted from authenticated exchange user fills only.
4. Keep offline simulation under tests only.
   - Completed: `PaperExecutionEngine` remains available via `ExecutionEngine` alias for unit tests; production startup no longer imports or instantiates it.

## Remaining paper/testing utilities

- `PaperExecutionEngine` and `ExecutionEngine` alias in `src/execution_engine.py`.
- Unit tests that validate offline simulated fills, historical PnL calculations, and safety regressions.

## Production invariants

- Startup logs `execution_mode=live_only`.
- `src.main.run()` always instantiates `LiveExecutionEngine`.
- `LiveExecutionEngine` is not a subclass of `PaperExecutionEngine` and does not expose `.paper`.
- Live `on_candle()` does not simulate candle-touch fills; it only polls exchange user fills.
