# Live execution safety

## Miért veszélyes a fake-live mód?

A fake-live azt jelenti, hogy a bot `PAPER_MODE=false` beállítással fut, de a kereskedési motor továbbra is paper logikát használ: belső `open_orders` listát tart fenn, gyertya `high/low` érintés alapján fillt szimulál, majd `_apply_fill()`-lel módosítja a lokális pozíciót. Ez live környezetben veszélyes, mert:

- nem kerül valódi order a Hyperliquid order bookba;
- a lokális pozíció eltérhet a tőzsdei pozíciótól;
- a risk logika hamis pozícióra és hamis PnL-re reagálhat;
- Telegramon és státuszban „LIVE” fillnek tűnhet egy olyan esemény, ami a tőzsdén soha nem történt meg;
- mikro-live közben a kezelő azt hiheti, hogy az order submit/cancel/fill útvonal validált, miközben csak paper szimuláció fut.

Kötelező szabály: ha `PAPER_MODE=false`, akkor candle-touch alapján tilos `_apply_fill()`-t használni valódi exchange order és fill sync nélkül.

## API wallet signer és `HL_ACCOUNT_ADDRESS`

A Hyperliquid live futtatásnál két szerep különül el:

- **API wallet signer / private key**: az a kulcs, amely aláírja az exchange műveleteket. Ezt soha nem szabad commitolni, logolni vagy preflight outputban kiírni.
- **`HL_ACCOUNT_ADDRESS`**: az a fő account/vault cím, amelynek az állapotát figyeljük és amelyhez a signer jogosultsága tartozik.

A signer címe és a fő account címe eltérhet. Ezért mikro-live előtt manuálisan ellenőrizni kell, hogy a használt API wallet ténylegesen a várt main account/vault számára jogosult-e megbízásokat beküldeni, és hogy a bot `HL_ACCOUNT_ADDRESS` értéke nem egy üres vagy hibás címre mutat.

## Main account address validálása

Mikro-live előtt ellenőrizd:

1. `ENV_PROFILE=live` és `HL_NETWORK=mainnet`.
2. A `HL_ACCOUNT_ADDRESS` ugyanaz a cím, amelyet a Hyperliquid felületen main accountként/vaultként látni szeretnél.
3. A preflight `accountValue` és `withdrawable` értéke pozitív.
4. Az `assetPositions_count` és `open_orders_count` ismert és vállalható állapotot mutat.
5. Nincs váratlan nyitott pozíció vagy order, amelyet a bot nem kezelne.

## Miért kell `accountValue` és `withdrawable` ellenőrzés?

Az `accountValue > 0` bizonyítja, hogy a bot a várt, finanszírozott account állapotát látja. A `withdrawable > 0` további sanity check: segít kiszűrni az üres, rossz vagy nem megfelelően elérhető accountot. Egyik sem helyettesíti a signer jogosultság validálását, de mindkettő hasznos indulás előtti védőkorlát.

## Live execution jelenlegi státusza

A repo jelenlegi Hyperliquid kliense read-only jellegű:

- market adat lekérés van;
- account state, positions és open orders lekérés van;
- valódi authenticated order submit nincs implementálva;
- exchange cancel nincs implementálva;
- user fills/fill sync nincs implementálva;
- reduce-only live order placement nincs implementálva;
- market/limit live order placement nincs implementálva.

Ezért a hard safety gate akkor is leállítja az indulást `live_execution_not_implemented` hibával, ha `PAPER_MODE=false`, `ENABLE_LIVE_TRADING=true` és `LIVE_EXECUTION_ENABLED=true`.

## Mikro-live indulási feltételek

A mikro-live csak akkor indulhat el, ha minden feltétel teljesül:

```env
ENV_PROFILE=live
HL_NETWORK=mainnet
PAPER_MODE=false
ENABLE_LIVE_TRADING=true
LIVE_EXECUTION_ENABLED=true
```

További feltételek:

- valódi exchange executor implementálva van order submit, cancel, open order sync és fill sync támogatással;
- reduce-only order támogatás validált;
- market/limit order placement támogatás validált;
- Telegram credentials jelen vannak;
- nincs `STOP_LIVE` / emergency stop file;
- `scripts/live_preflight_check.sh` `LIVE PREFLIGHT OK` eredménnyel fut;
- a live service indulás előtt továbbra is disabled/stopped, és csak kézi jóváhagyással indítható.

Amíg a fenti exchange executor nincs kész, a botnak live módban fail-fast módon kell megállnia, nem pedig paper szimulációval futnia.
