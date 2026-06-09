# Trade Expectancy Diagnostics

Use the read-only expectancy reporter to explain why the live Hyperliquid adaptive grid bot can remain net negative even when the headline win rate is near 52%.

```bash
python -m scripts.trade_expectancy_report --limit 300
```

By default the reporter reads `logs/trades.jsonl`, reconstructs closed lots from the fill ledger, and prints the most recent closed-trade window plus an all-trades window. To print the requested rolling windows together, run:

```bash
python -m scripts.trade_expectancy_report --windows 100,200,300
```

Optional flags:

- `--ledger PATH` points at a different `trades.jsonl` or `trades.csv` file.
- `--output docs/trade_expectancy_diagnostics.md` writes the Markdown report to disk.
- `--no-all` suppresses the all-trades window.

The report includes:

- Core expectancy metrics: closed count, win rate, average/median winner and loser, largest win/loss, gross profit factor, profit factor after fees, gross PnL, net PnL, fees, expectancy per trade, and fee drag.
- Hold-time diagnostics when the ledger contains enough entry/exit timing to reconstruct them.
- Fill-quality diagnostics: dust trades under $2 and below-min-notional fills under $10.
- Segment tables grouped by side, UTC hour of day, regime, orderbook classification, and prediction bias when those fields are present in the ledger.
- A Telegram/performance `EXPECTANCY` block with average win, average loss, win/loss ratio, profit factor after fees, fee drag, and dust count.
- A recommendation section identifying whether the issue is win rate, average win/loss ratio, fee drag, dust fills, or the weaker side.

This utility does not modify orders, positions, strategy parameters, or live trading behavior.
