# hyperliquid-adaptive-grid-bot

Adaptive Neutral Futures Grid bot Hyperliquid perpetual piacra, modularis Python felépítéssel, risk management fókuszszal, Telegram monitorozással és paper trading támogatással.

## Fő cél
A projekt a klasszikus market making/grid logikát alakítja át Hyperliquid futures környezetre úgy, hogy:
- **adaptív** legyen (piaci rezsim alapján paraméterezés),
- **biztonságos** legyen (drawdown és daily loss limitek),
- **átláthatóan monitorozható** legyen (log + Telegram),
- **könnyen bővíthető** legyen (moduláris `src/` struktúra).

## Fontos figyelmeztetés
- Kezdj **TESTNET** módban.
- Soha ne commitálj privát kulcsot.
- 500 USD tőkéhez konzervatív paraméterezés javasolt (alacsony leverage, kisebb order size).
- Ez a repository egy production-ready irányba szervezett **alap keretrendszer**, éles kereskedés előtt kötelező saját tesztelés.

## Könyvtárstruktúra

```text
.
├── config/
│   ├── aggressive.env
│   ├── conservative.env
│   └── paper.env
├── scripts/
│   ├── start_hyperliquid_paper.sh
│   └── start_hyperliquid_production.sh
├── src/
│   ├── config.py
│   ├── execution_engine.py
│   ├── grid_manager.py
│   ├── hyperliquid_client.py
│   ├── main.py
│   ├── position_manager.py
│   ├── regime_detector.py
│   ├── risk_manager.py
│   ├── startup_validation.py
│   ├── strategy_orchestrator.py
│   ├── telegram_handler.py
│   ├── types.py
│   └── utils.py
├── .env.example
└── requirements.txt
```

## Telepítés

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Konfiguráció
A fő paraméterek `.env`-ből olvasódnak:
- hálózat: `HL_NETWORK=testnet|mainnet`
- hitelesítés: `HL_PRIVATE_KEY`, `HL_ACCOUNT_ADDRESS`
- stratégia: `GRID_LEVELS`, `GRID_SPACING_PCT`, `BASE_LEVERAGE`
- risk: `MAX_DRAWDOWN_PCT`, `DAILY_LOSS_LIMIT_PCT`, `EMERGENCY_STOP`
- futás: `BOT_TICK_SECONDS`, `STATE_FILE`, `LOG_LEVEL`

Profilok:
- `config/paper.env`
- `config/conservative.env`
- `config/aggressive.env`

## Fő modulok
- `hyperliquid_client.py`: Hyperliquid kliens és alap account műveletek.
- `regime_detector.py`: RANGE / TREND_UP / TREND_DOWN / RISK_OFF / HIGH_VOL.
- `risk_manager.py`: drawdown + napi veszteség limitek + preservation mód.
- `grid_manager.py`: neutral long+short grid szintek, volatilitás-alapú spacing.
- `execution_engine.py`: cancel/replace order workflow helye.
- `strategy_orchestrator.py`: modulok összekötése tick-alapon.
- `telegram_handler.py`: állapotüzenetek Telegramra.

## Indítás

Paper:
```bash
./scripts/start_hyperliquid_paper.sh
```

Production (csak saját felelősségre):
```bash
./scripts/start_hyperliquid_production.sh
```

## Következő lépések
1. Valós Hyperliquid SDK hívások bekötése a `hyperliquid_client.py` és `execution_engine.py` fájlokban.
2. Backtester/paper execution bővítése trade-by-trade elszámolással.
3. Retry és rate-limit middleware hozzáadása.
4. Telegram command handler bővítése (`/status`, `/pause`, `/resume`, `/risk`).
