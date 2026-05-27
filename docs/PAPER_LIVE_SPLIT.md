# Paper / Live split üzemeltetési struktúra

## Miért marad egyetlen Git repository?
Egy közös repository biztosítja, hogy a stratégiai kód, hibajavítások és verziókövetés egységes maradjon. A futtatási környezet mégis külön van választva két VPS mappára, így a paper és a live példány állapota nem keveredik.

## Könyvtárstruktúra VPS-en
- `/root/hyperliquid-adaptive-grid-bot-paper`
- `/root/hyperliquid-adaptive-grid-bot-live`

Mindkét mappa saját relatív almappákat használ (`state/`, `logs/`, `data/`), így nem írják egymás fájljait.

## Systemd service nevek
- `hyperliquid-grid-bot-paper.service`
- `hyperliquid-grid-bot-live.service`

A live service template alapból safe módban van előkészítve (a tényleges env-ben is így kell maradnia, amíg nincs kézi jóváhagyás).

## Lock és STOP fájlok különválasztása
- Paper lock: `/tmp/hyperliquid_paper.lock`
- Live lock: `/tmp/hyperliquid_live.lock`
- Paper STOP: `state/STOP_PAPER`
- Live STOP: `state/STOP_LIVE`

Állapotfájlok:
- Paper: `state/paper_bot_state.json`
- Live: `state/live_bot_state.json`

## Paper indítása
1. Paper könyvtárban töltsd be a paper konfigurációt.
2. Indítsd systemd-vel:
   - `systemctl enable --now hyperliquid-grid-bot-paper.service`
3. Ellenőrzés:
   - `systemctl status hyperliquid-grid-bot-paper.service`
   - logok és state a paper mappában keletkeznek.

## Live előkészítése safe módban
1. Másold a `config/live.env.example` fájlt `config/live.env` néven kizárólag a live mappában.
2. Töltsd ki a szükséges mezőket (kulcsokat csak lokálisan, soha ne commitold).
3. Hagyd ezeket az értékeket:
   - `PAPER_MODE=true`
   - `ENABLE_LIVE_TRADING=false`
4. Live service telepíthető, de ne legyen automatikusan élesítve.

## Hogyan ellenőrizd, hogy a live nincs élesítve?
- A live env-ben `ENABLE_LIVE_TRADING=false`.
- A live env-ben `PAPER_MODE=true`.
- Nincs valódi live kulcs commitolva a repository-ba.
- A `hyperliquid-grid-bot-live.service` nincs `enable --now` módban indítva alapértelmezetten.

## Mikor jöhet mikro-live?
Csak ezek teljesülése után:
- 48-72 óra stabil paper futás.
- Nincs crash.
- Nincs tartós risk `BLOCKED` állapot.
- Fee/profit arány kontrollált.
- Telegram fill alert stabilan működik.
- Max pozíció továbbra is mikro szinten tartva.
- `ENABLE_LIVE_TRADING=true` csak külön kézi döntéssel állítható.
