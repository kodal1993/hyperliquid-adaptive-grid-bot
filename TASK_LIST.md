TASK: Hozd létre az új Hyperliquid Adaptive Futures Grid Bot projektetProjekt név javaslat: hyperliquid-adaptive-grid-botCél:
Átalakítani a meglévő Uniswap V3 market maker bot logikáját egy könnyen kezelhető, robusztus, adaptív Futures Grid Bot-tá Hyperliquid perpetual kereskedésre. Fő hangsúly a modularitáson, erős risk managementen, Telegram monitorozáson és live-only Hyperliquid végrehajtáson.Projekt KövetelményekTechnikai stack:Python 3.11+
Hivatalos Hyperliquid Python SDK (hyperliquid-python-sdk)
python-dotenv, ccxt (opcionális fallback), pandas, numpy
Telegram bot integráció (python-telegram-bot vagy requests)
Modularitás (src/ mappa)

Fő stratégia:Adaptive Neutral Futures Grid (Long + Short sub-grid)
Dinamikus grid paraméterek a piaci rezsim alapján (RANGE / TREND_UP / TREND_DOWN / RISK_OFF / HIGH_VOL)
Leverage támogatás (3x–8x, konfigurálható)
Erős drawdown védelem és capital preservation mód
Automatikus grid újragenerálás és pozíció menedzsment

Task Lista (lépésről lépésre)Projekt inicializálásHozz létre tiszta új repository struktúrát
requirements.txt fájl létrehozása (hyperliquid-python-sdk, python-dotenv, pandas, numpy, requests, python-telegram-bot stb.)
.env.example és több profil (live, conservative, aggressive)
README.md részletesen (telepítés, konfiguráció, figyelmeztetések)

Konfiguráció és Hyperliquid integráció.env támogatás: private_key, account_address, TESTNET/MAINNET kapcsoló
hyperliquid_client.py modul: Info és Exchange osztály inicializálása, wallet kezelés, testnet támogatás
Alapfunkciók: egyenleg lekérdezés, pozíció lekérdezés, leverage beállítás, open orders lekérdezés
Startup validation script (kapcsolat teszt + egyenleg + instrument info)

Meglévő logika átmentése (refactoring)regime_detector.py – megtartani és finomhangolni (TREND, RANGE, RISK_OFF, volatilitás osztályozás)
risk_manager.py – drawdown számítás, daily loss limit, max position size, capital preservation mód
telegram_handler.py – meglévő parancsok + új grid-specifikus parancsok
utils.py és logging rendszer megtartása

Új modulok létrehozásagrid_manager.py → fő grid logika (neutral grid, dynamic spacing ATR vagy volatility alapján, grid szintek generálása)
execution_engine.py → Hyperliquid specifikus order placement (limit orders, batch ordering, cancel & replace)
position_manager.py → jelenlegi pozíciók követése, PNL számítás, rebalancing
strategy_orchestrator.py → összeköti a rezsim detektort + risk managert + grid managert

Stratégia logikaRANGE rezsimben: szűk, sűrű grid mindkét irányba
TREND rezsimben: bias (long bias vagy short bias grid) vagy grid kikapcsolás
HIGH_VOL / RISK_OFF: nagyon széles grid vagy teljes stop
Automatikus profit taking + részleges pozíció zárás
Re-entry logika megtartása ahol értelmes

Risk & Money ManagementMax drawdown limit (pl. 25-40%)
Egy trade max kockázat (% of equity)
Leverage dinamikus állítás
Emergency stop funkció

Backtesting és Paper TradingPaper trading mód (szimulált egyenleg + szimulált execution)
Egyszerű historikus backtester keret (BTC és ETH perpetual adatokkal)

Futtatás és Monitorozásmain.py – fő loop (5-15 másodpercenként tick)
start_hyperliquid_production.sh live-only production script
Részletes logging + állapot mentés (JSON)

