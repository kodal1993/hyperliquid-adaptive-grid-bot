# Changelog

## Kísérleti napló: Funding carry 2.0 kártya (B irány)

- `docs/KISERLETEK.md`: új kísérleti kártya, kód nélkül. Eszközválasztás a fedezhető eszközök közül, rezsimkapcsoló, 65%-os fedezettség újrasúlyozással, maker-megbízások; költségpadló és várható hozam forgatókönyvenként; kapu (`funding_scan`, 90 nap: legalább 10%/év medián és legalább 70% pozitív óra); siker: HLP + 2 százalékpont; határidő az indulástól 30 nap. A megfontolt alternatívák és hogy miért nem jobbak most.

## Kísérleti napló: a pump.fun sniper lezárva (kudarc)

- 2026-10-23 19:04: S10_bennfentes −14,2% ±8,8 (n=90) és S13_amm −54,5% ±10,7 (n=99), mindkettő `NEGATIV`; az S14 végső száma −24,9% ±4,6 (n=161).
- A sniper-kísérlet lezárva: összesen 350 pozíció, −10,8 SOL papíron. Ingyenes, késleltetett adattal a pump.fun nem profitforrás nekünk; ugyanerre a piacra új stratégiát nem írunk. Aktív kísérlet: a HYPE carry (határidő 10-28).

## Kísérleti napló: S14_volumen lezárva (kudarc, korai leállítás)

- 2026-10-22 18:04: S14 −35,9% ±10,7 (n=61), ítélet `NEGATIV`. A `gyors` kiszállás +8,8 ±7,6 százalékponttal jobb, de az is −27,1%. A 2. szabály szerint korán leállítva, a `PAPER_SNIPER_STRATS`-ból kivéve; aktív: S10_bennfentes, S13_amm.

## Paper sniper: `/wallets` „szerencse vagy tudás” hibahatárral; „nan” helyett „-”

- A 3. pont eddig minden pozitív különbségre azt írta, hogy „van kitartó tudás”, például +0,3 ±7 százalékpontra is. Mostantól az átlagos hozam és a nyerő arány különbsége is a 95%-os hibahatárral együtt jelenik meg, és csak akkor „jobbak (statisztikailag)”, ha a hibahatár nem metszi a 0-t.
- Adat nélküli medián (pl. görbe-SOL) „nan” helyett „-”.
- `docs/KISERLETEK.md`: új szabályok: korai leállítás (legalább 50 lezárt pozíción `NEGATIV` → leállítható a határidő előtt; korai siker nincs) és kódstop futó kísérlet alatt; a sniper-kártyán az ellenőrzési pontok.
- `docs/KISERLETEK.md`: az S8-lezárás „+18,5 százalékpontos tudás” állítása javítva. A mayhem-ügynök hamis PnL-je fújta fel; a túladás-szűrés után az átlagban nincs bizonyított különbség, a nyerő arányban van.

## Paper sniper: túladott tárca-pozíciók kizárva (több tokent ad el, mint amennyit a görbén vett)

- **Mérés:** ha egy tárca egy tokenből több tokent ad el, mint amennyit a görbén vett, a különbség tárca-pozíciónként számolódik. Ilyen a mayhem-ügynök extra kínálata, egy átutalással kapott token, vagy az RPC-kiesés miatt nem látott vétel.
- **Elszámolás:** az ilyen pozíció (a túladott mennyiség több, mint a legnagyobb tartott mennyiség 1%-a) kimarad a PnL-ből, a viselkedésnaplóból és a követő-mérésből, mert a PnL-je hamis volna. A tárca-rekord két új számlálót kap: túladott és tiszta pozíciók a bevezetés óta.
- **Okos-besorolás:** aki az új adatokban legalább 3-szor, és a pozíciói legalább 20%-ában túlad, az nem lehet okos. A token-napló „okos tárcák” jele, a `/wallets` és a `/diag` okos-listája így nem tartalmazza.
- **Riport:** a `/wallets` 5. pontja tárcánként mutatja a túladott pozíciók arányát, és külön listázza a túladás miatt kizárt tárcákat a régi (hamis) PnL-jükkel; a `/diag` a kihagyott pozíciók számát és a legnagyobb kizártakat.
- A korábbi adatbázis-rekordok PnL-je nem változik.

## Paper sniper: mayhem-tokenek és nem szabványos görbék külön; nyitott-plafon 600

- **Valódi SOL és mayhem-jelző az eseményből:** a `decode_trade_event` a `real_sol_reserves` (113. bájt) és a `mayhem_mode` (az `ix_name` string után) mezőt is kiolvassa. A token-napló „SOL a görbében” jele (`rsol`) és a tárcanapló „görbe SOL” oszlopa ebből jön, nem a „virtuális − 30” képletből, ami a nem szabványos (pl. mayhem) görbéken hibás volt (valódi minták: 2,66 és 11 SOL induló virtuális SOL).
- **Görbe-napló** (`state/paper_sniper_curves.csv`): tokenenként a görbe induló virtuális SOL-ja és a mayhem-jelző. A riportok mint szerint illesztik, így a meglévő CSV-k sémája nem változik.
  - `/tokens`: új sáv (ismeretlen / szabványos / mayhem / egyéb görbe); az S14-szakasz a jel mayhem- és egyéb görbéjű tokenjeit is megszámolja.
  - `/wallets`: új sáv „a token görbéje”; az 5. pontban tárcánként a mayhem-tokenek aránya.
  - `/sniper`: stratégiánként görbe szerinti bontás.
  - `/diag`: az elszámolt tokenek görbék szerinti megoszlása.
  - Az `/export` a görbe-naplót is tartalmazza.
- **`PAPER_SNIPER_MAX_OPEN` alapértéke 120 → 600:** az S14 kb. 35 belépés/óra mellett 24 óráig tarthat nyitva, a 120-as plafon pár órán belül kihagyásokat okozott volna.

## Kísérleti napló: az S13 és az S14 2026-10-22 ~10:31-től fut; második valódi TradeEvent a tesztben

- `docs/KISERLETEK.md` és a docstring: az S13 és az S14 ténylegesen 2026-10-22 ~10:31-től fut (a #127 telepítése a javított stratégialistával), az eredményeik ettől számítanak. Első működési adat (12:21): S14 61 belépés, S13 4 belépés 69 értékelt graduációból, a kihagyási okokkal.
- Második valódi TradeEvent-sor a dekóder-tesztben (slot 459983200): szabványos görbe (virtuális − valódi SOL = 30) és nem nulla díj (95 + 30 bps). Így a görbe-feltevés és a díjmező eltolása is valódi adaton van igazolva; az első valódi sor mayhem-módú volt, 0 díjjal.

## Paper sniper: S8 lezárva, tárca-elszámolás díjakkal és PumpSwap-áron, stratégia-figyelmeztetés, nyitott pozíciók stratégiánként

- **S8_masol lezárva (kudarc), a kódból kivezetve:** 38 144 követő-pozíció után sincs másolható tárca (átlag −12,5%; az okos tárcák követve −2,8%). A jó tárcák előnye a sebesség. A `PAPER_SNIPER_STRATS` régi listájában maradt `S8_masol`-t a sniper figyelmen kívül hagyja. A követő-mérés (`/wallets` 4. pont) marad.
- **Tárca-elszámolás:**
  - a tárca saját díjai is levonódnak: a TradeEvent `fee` + `creator_fee` mezője (a pump.fun SDK szerint vételkor a görbe SOL-ján felül fizeti, eladáskor abból vonják le). Rövidebb, régi eseménynél a konfig szerinti 1,25%/irány;
  - ha a token a követési ablakban graduál, az elszámolás megvárja a PumpSwap-árat 15 perccel a graduáció után (legfeljebb 30 percig), és a maradékot azon, AMM-díjjal értékeli, nem a görbe csúcsárán. Ugyanez a követő-hozam kiszállási áránál.
  - A `/wallets` fejléce ezt írja le; a 2026-10-22 előtti sorok optimisták, a napló kb. fél nap alatt lecserélődik.
- **Telegram:** a hosszú üzenetek sorhatáron darabolódnak (eddig 3800 karakterenként, így egy szám is kettétörhetett, pl. „3” + „8144”).
- **Stratégia-lista figyelmeztetés:** induláskor, a `/diag`-ban és a Telegram-jelentésben, ha a `PAPER_SNIPER_STRATS` ismeretlen (pl. kivezetett) nevet tartalmaz, ha egy stratégiát kizár, vagy ha a mostani konfig mást futtatna, mint a futó sniper (újraindítás kell). A futó példány listája az állapotfájlba kerül.
- **Nyitott pozíciók stratégiánként:** a `/sniper` riportban, a Telegram-jelentés táblázatában és a `/diag` belépési táblájában. A futó, de még lezárt pozíció nélküli stratégia is megjelenik; a kivezetett stratégiák még nyitott pozíciói külön sorban.
- `docs/KISERLETEK.md`: az S13 és az S14 2026-10-22 10:25-ig egyszer sem futott (a `/diag` szerint 0 belépés, 0 S13-értékelés: a futó sniper listája kizárta őket); róluk eddig nincs adat.
- **Teszt egy valódi TradeEvent-sorra** (a VPS `/diag` mintája, slot 459966561): a várt értékek a hivatalos IDL teljes borsh-elemzéséből (391 bájt). Ez egy mayhem-módú token: díj 0, a görbe induló virtuális SOL-ja kb. 2,66, nem 30.

## Paper sniper: 50-es ítélet-küszöb, 1,2%-os AMM-díj, S14-jel 60 mp-ről, tesztek, izolált futtatás

- **Ítélet:** a riport, a Telegram-jelentés és a kiszállás-összevetés legalább 50 pozíción dönt (eddig 30), a `docs/KISERLETEK.md` sniper-kártyája szerint (`VERDICT_MIN_N`).
- **AMM-díj:** a `PAPER_SNIPER_AMM_FEE_BPS` alapértéke 30 → 120 bps (a PumpSwap friss graduált sávja kb. 1,2%/irány). Az eddigi S13 sorok és a graduáció után zárt S14 (`+amm`) sorok optimisták, pozíciónként kb. 1,8 százalékponttal. A `/diag` beállítás-sora mutatja az AMM-díjat.
- **`tokens`:** az S14-jel (30 mp-nél legalább 25 SOL vásárlás) kimenetele a 60 mp-es pillanatkép árától is, mert a belépés a késés miatt kb. ott történik. A két pillanatkép mint szerint párosítva, tanító és ellenőrző részre bontva, a 60 mp-es alapvonallal együtt.
- **Tesztek** (`tests/test_paper_sniper.py`):
  - `decode_trade_event` egy elmentett `Program data:` sorra (`tests/fixtures/pump_trade_events.json`). A sor egyelőre a pump.fun hivatalos IDL-je (`@pump-fun/pump-sdk` 4.2.0, 36 mezős TradeEvent) szerint kódolt, mert a fejlesztői környezet nem éri el a Solana RPC-t; a valódi sor a `/diag` mintájából kerül mellé. Független ellenőrzés: a görbe szorzata és a valódi SOL-tartalék;
  - vétel és eladás oda-vissza kb. −2 × díj;
  - `poll_once` kiszállások: TP, SL, idő, S14 gyors és alap trail, graduáció → a görbe-stratégia zár, az S14 a PumpSwap-on folytatja 1,2%-os díjjal és csúszással;
  - az 50-es ítélet-küszöb; a `tokens` S14 60 mp-es szakasza.
- A `/diag` óránként elment és kiír egy valódi `TradeEvent` mintasort (slot, tranzakció), a teszt-fixture-höz.
- **Izolált futtatás** (`deploy/install_paper_sniper.sh`, egyszer, root-ként):
  - `paper-sniper` felhasználó, állapot a `/var/lib/paper-sniper`-ben, konfig a `/etc/paper-sniper/paper_sniper.env`-ben (a mostani `PAPER_SNIPER_*` és Solana/PumpPortal URL sorokból; privát kulcs és Telegram-token nélkül);
  - a systemd unit a repóból csak a `scripts/` és a `.venv/` könyvtárat látja: a `.env`, a `.env.bak*` és a `config/live.env` a szolgáltatás számára nem létezik;
  - `systemd-analyze security`: 9,4 (UNSAFE) → 4,3 (OK). Visszaállítás: `--vissza`.
  - A vezérlő `/set PAPER_SNIPER_*` parancsa ekkor a sniper saját konfigjába ír (megtartja a jogosultságát), az `/export` onnan olvas, a sniper parancsai a vezérlő környezete (Telegram-token, kulcsok) nélkül futnak.
  - A telepítő futtatásáig minden a régi módon megy.
- A lock-fájl a `/tmp` helyett az állapot-könyvtárban van (a systemd `PrivateTmp`-je miatt).

## Telegram: `/set` és `/unset` — papír-beállítások a telefonról

- `/set` listázza, `/set KULCS=ÉRTÉK` beírja vagy cseréli, `/unset KULCS` törli a beállítást a `config/live.env`-ben. Érvényes a `/restart_sniper` vagy `/restart_carry` után.
- **Csak** `PAPER_SNIPER_*`, `PAPER_CARRY_*`, `CARRY_*` és `TELEGRAM_REPORT_INTERVAL_HOURS` állítható. Ami `KEY`, `TOKEN`, `SECRET`, `URL`, `PRIVATE`, `PASS` vagy `WALLET` szót tartalmaz, soha.
- **Érték:** csak betű, szám és `_ . , : -`, legfeljebb 200 karakter.
- A fájl többi sorához nem nyúl. Atomikusan ír, 600-as jogosultsággal. Figyelmeztet, ha ugyanez a kulcs a `.env`-ben is szerepel, mert az felülírja.

## Paper sniper: S6 és S12 kivezetve, puffer- és memória-optimalizálás

- **Kivezetve** (statisztikailag lezárt kudarcok, `docs/KISERLETEK.md`):
  - `S6_okos` / `S6_okos2`: −19,8% ±7,0 (n=196) és −25,1% ±10,2 (n=77);
  - `S12_futtat`: −55,4% ±15,8 (n=10).

  Az okos tárcák száma a token-napló jeleként marad.
- **Puffer:** csak azokat a tokeneket puffereli, amelyeknek a `CreateEvent`-je már megjött a Solana RPC-n, de a PumpPortal create üzenete még nem. Eddig minden régi token eseményét is 30 mp-ig tárolta (a mérésen 20 000 eseményt 5000 tokenre).
- **Memória:** elszámolás után a tokenből eltűnik a teljes kereskedési előzmény (tárcák, ártörténet, pillanatképek). A legfeljebb 5000 memóriában tartott token így csak a könnyű mezőket őrzi.
- **Tömör JSON** az állapot- és adatbázis-fájlokban.
- **Halott kód törölve:** a PumpPortal feliratkozási sorai (`sub_q`, `unsub_q`, `subs_sent`).
- **A riport „Kihagyva / nem sikerült belépni” sora** csak az aktív stratégiákat mutatja.
- **Mérés:** az RPC-értesítések feldolgozása kb. 40 000-ről 55 000/mp-re gyorsult (a valós forgalom kb. 350/mp), a késést tehát az RPC okozza, nem a kód.

## Paper sniper: token-napló és `tokens` elemzés — a nyerő tokenek korai jelei

- Minden új tokenről pillanatkép 30 és 60 mp-es korban, láncon mérve (75 és 150 slot a `CreateEvent` után), így az RPC-késés nem torzítja.
  - **A pillanatkép tartalma:** görbe, vevők, vételek és eladások, vásárolt SOL, a legnagyobb vevő aránya, dev-vétel és -részesedés, dev eladott-e, bundle-arány, klaszteres-e, másolt név, a dev korábbi indításai és graduációi, okos és ismert vesztes tárcák száma.
  - **Kimenetel:** a pillanatkép árától mért legnagyobb és végső szorzó a követési ablak végéig.
  - A naplók: `state/paper_sniper_tokens.csv` és a graduációk a `state/paper_sniper_migrations.csv`-ben.
- `paper_sniper tokens [--h 30|60]` és Telegram `/tokens`:
  - jelenként és sávonként a nyerő arány (legalább 3x vagy graduáció) a tanító (első 70%) és az ellenőrző (utolsó 30%) részen, az emelés és a 15 perces átlagos végső szorzó;
  - csillag csak akkor, ha mindkét részen legalább 1,5-szörös az emelés;
  - a legjobb 3 tanító sáv eredménye az ellenőrző részen.
- A `/export` tartalmazza a token-elemzést, a token-napló utolsó 200 ezer sorát és a graduációs naplót.

## Paper sniper: S13_amm — graduáció utáni belépés (késés-független, ingyenes)

- A PumpPortal graduációs eseménye után `PAPER_SNIPER_S13_DELAY_MIN` (5) perccel a DexScreener-adatok alapján dönt a belépésről.
  - **Feltételek:** a legnagyobb likviditású SOL-pár likviditása ≥ `S13_MIN_LIQ_USD` (8000 $); az 5 perces forgalom ≥ `S13_MIN_VOL5_USD` (3000 $); a vételek ≥ `S13_BUY_RATIO` (1,2) × az eladások; az 5 perces árváltozás > 0; nem klaszteres vagy másolt token.
  - **Indexelés:** ha a pár még nincs indexelve, 3 percenként legfeljebb kétszer újrapróbálja.
- **Belépés és értékelés** a PumpSwap áron: AMM-díj, plusz csúszás a pool SOL-tartalékából (állandó szorzat), az S12 graduáció utáni értékelésénél is. Kiszállás, mint az S12-nél: csúcstól −50%, legfeljebb 24 óra.
- A `dex_pairs()` a DexScreenerből likviditást, SOL-tartalékot, 5 perces forgalmat, vételeket és eladásokat, árváltozást ad. A `diag` kiírja az S13 kihagyási okait.
- `docs/KISERLETEK.md`:
  - az S6 lezárva (statisztikailag negatív);
  - az S8 és az S10 felfüggesztve (nincs valós idejű adat);
  - aktív: S12 és S13, határidő 2026-11-04.

## Paper sniper: korai vevők láncon mért idővel (slot), RPC-késésmérés

- A nyilvános Solana RPC lassabb a PumpPortalnál, ezért a korai vételek az érkezési idő szerinti 3 mp-es ablakon túl jöttek meg. Emiatt nem lett klaszter, és a tárca-pontozás korai kizárása is pontatlan volt.
- A „korai” mostantól láncon mért: legfeljebb `EARLY_SLOTS` (7, ~2,8 mp) slot a token létrehozása után. A létrehozás slotját a pump.fun `CreateEvent` (`1b72a94ddeeb6376`) adja a Solana RPC-ről. Ha nincs slot, a régi, érkezési idős szabály a tartalék.
- A klaszter-elemzés megvárja, hogy a stream túljusson a korai ablakon (legfeljebb 30 mp). A tárca-pontozás a korai vevőket slot szerint zárja ki.
- A `diag` kiírja a CreateEvent-ek számát és az RPC átlagos késését (a blokkidőhöz képest).

## Paper sniper: a create előtt érkező korai kereskedések pufferelése

- A Solana RPC gyorsabb a PumpPortalnál: egy új token első vételei (bundle-ök, korai sniperek) a PumpPortal create üzenete előtt érkeznek, és eddig elvesztek. Ezért nem lett egyetlen klaszter sem (`tarca-parok: 0`).
- A még ismeretlen tokenek TradeEvent-jei 30 mp-ig pufferben maradnak (tokenenként legfeljebb 50). A create után az eredeti érkezési idővel visszajátszódnak, így a korai vevők és a klaszter-felismerés helyesen működnek. A `diag` kiírja a visszajátszott események számát.

## Paper sniper: kereskedések a Solana RPC-ről (a PumpPortal kereskedés-adata fizetős lett)

- A PumpPortal `subscribeTokenTrade` / `subscribeAccountTrade` csak legalább 0,02 SOL-lal feltöltött API-kulccsal működik. Ezért eddig egyetlen kereskedés-üzenet sem jött, és a tárcakövetés adat nélkül futott.
- **Új adatforrás:** a Solana RPC WebSocket `logsSubscribe`-ja a pump.fun programra. A „Program data:” sorokban lévő `TradeEvent` eseményeket (`bddb7fd34ee661ee`) dekódoljuk: mint, SOL, token, vétel/eladás, tárca, virtuális tartalékok. Ingyenes. URL: `SOLANA_WS_URL`, vagy a `SOLANA_RPC_URL` wss-változata. A PumpPortal továbbra is az új tokeneket és a graduációkat adja.
- Az elutasított PumpPortal-feliratkozásokat a bot nem küldi többé. A követett tárcák eladását nem követett tokenen is figyeli (`masol` kiszállás).
- WebSocket-diagnosztika a `diag`-ban:
  - RPC-értesítések és TradeEvent-ek száma, újrakapcsolódások, utolsó hiba;
  - PumpPortal-üzenettípusok és az utolsó válaszok;
  - a bináris keretek dekódolása.

## Paper sniper: csak tárcakövetés + S12_futtat (graduáció után is)

- **Kivezetve:** S0–S5, S7, S9, a metaadat-letöltés (IPFS), a trail/gyors/reszl/szoros/futtat kiszállási változatok, a `report --javitott` és a kiürültgörbe-diagnosztika. A régi sorok a CSV-ben maradnak, de a riportok csak az aktív stratégiákat mutatják.
- **Marad:** S6_okos, S6_okos2, S8_masol, S10_bennfentes; a tárca-adatbázis, a követhetőség, a bennfentes-klaszterek, a viselkedésnapló, valamint az `alap` és a `masol` kiszállás.
- **Új `S12_futtat`:**
  - belépés, ha a görbe legalább 50%-on van, nincs klaszter, nem másolt név, legalább 25 vevő, és eladás/vétel < 0,7;
  - **graduáció után is tartjuk:** az ár a DexScreener nyilvános API-jából jön, a legnagyobb likviditású SOL-pár `priceNative` értéke, 30 mp-enként, 30-asával lekérdezve; az AMM-díj `PAPER_SNIPER_AMM_FEE_BPS` (30);
  - kiszállás a csúcstól −50%-nál, legfeljebb 24 óra, TP-plafon nélkül; a záráskori ok `+amm` jelölést kap.
- A `docs/KISERLETEK.md` sniper-kártyája frissült. S12 határideje: 2026-11-04.

## Paper sniper: S10_bennfentes — a bennfentesek követése, kiszállás az első eladásukkor

- `S10_bennfentes`: belépés a klaszteres tokenbe a felismerés után (3 mp + `COPY_LAT`), ha még egyik bennfentes sem adott el; ha már eladtak, kimarad. A minta aránya `PAPER_SNIPER_SAMPLE_S10` (0,3).
- A `masol` kiszállás most több követett tárcát is kezel: az S10 és az S9_klaszteres pozíciók az **első** bennfentes eladásakor zárnak. Ha a követett tárca már a belépés előtt eladott, a pozíció azonnal zárásra jelölődik.
- A `wallets` 4. pontja: a bennfentes-klaszter tárcák követő hozama. A `docs/KISERLETEK.md` kártyája kiegészült.

## Paper sniper: bennfentes-klaszterek felismerése a láncon (S7_tiszta, S9)

- RPC nélkül, a kereskedés-üzenetekből. Korai vevők = a token első 3 mp-ének vevői, a dev nélkül.
  - **Visszatérő csoport:** két korai vevő legalább `PAPER_SNIPER_CLUSTER_MIN` (3) korábbi tokenben is együtt volt korai.
  - **Dev-barát:** a korai vevő ugyanannak a devnek legalább 2 korábbi tokenjében is korai volt.

  Klaszteres a token, ha legalább 2 csoporttag vagy legalább 1 dev-barát van benne. A jelet csak a korábbi tokenekből számoljuk, így nincs előrelátás.
- Új stratégiák:
  - `S7_tiszta`: S7_organikus klaszter nélkül;
  - `S9_klaszter_nelkul` és `S9_klaszteres`: 10 mp-es minta (`PAPER_SNIPER_SAMPLE_S9`, 10%), egymás kontrolljai.
- A `diag` mutatja a klaszter-arányt és a legerősebb visszatérő párokat, a Telegram a klaszteres tokenek arányát. A `docs/KISERLETEK.md` sniper-kártyája kiegészült.

## Paper sniper: követhetőségi pontszám, S8_masol, „masol” kiszállás

- A tárca saját PnL-je félrevezet: egy gyors bot mindig nyerőnek látszik, de 1 mp késéssel követve már veszteséges. Ezért minden tárca-pozícióhoz kiszámoljuk a **követő hozamot** is: belépés `COPY_LAT` mp-cel a tárca vétele után, az akkor érvényes áron; kiszállás az első eladása után `COPY_LAT` mp-cel (vagy az ablak végén); a mi díjainkkal és prioritási díjunkkal. A hozam +500%-nál le van vágva.
- **Másolható** egy tárca, ha legalább `COPY_MIN_N` (8) követő-pozíciója van, és az átlagos követő hozam − 2 standard hiba > 0.
- `S8_masol`: belépés 1 mp késéssel, ha egy másolható tárca vásárol.
- Új `masol` kiszállási változat az S6 és az S8 pozícióira: kiszállás, amikor a követett tárca elad.
- A `wallets` elemzés 4. pontja: tárcák követő hozama, a saját PnL szerint okos tárcák követve, a top másolható tárcák. A Telegram-jelentés kiírja a másolható tárcák számát.

## Paper sniper: okos tárcák viselkedésnaplója és elemzése (`wallets`, `/wallets`)

- Viselkedésnapló (`state/paper_sniper_wallet_trades.csv`) minden követési ablak végén. Benne van a legalább 2 korábbi tokenen kereskedő tárcák minden pozíciója, valamint 5% véletlen minta a többiből alapvonalként. Mezők: a token kora a belépéskor, valódi SOL a görbében, tétel, tartási idő, eladott hányad, vételek/eladások száma, a token vevőszáma és korai (bundle) része, PnL. `PAPER_SNIPER_WALLET_LOG_MB` (300) felett archiválódik.
- `paper_sniper wallets` / Telegram `/wallets`:
  - az okos és a többi tárca viselkedésprofilja;
  - „mi működik” táblák belépési kor, görbeállás, tartás, tétel és bundle-arány szerint;
  - szerencse vagy tudás teszt: az első fele alapján címkézett tárcák második felének eredménye;
  - a legjobb okos tárcák mediánjai.
- A `/export` tartalmazza a `wallets` elemzést és a napló utolsó 200 ezer sorát.

## Paper sniper: tárca-adatbázis (okos tárcák), bundle-szűrő, reális díj

- Kereskedés-követés: minden új tokenre `subscribeTokenTrade` az első `PAPER_SNIPER_TRADE_WINDOW_MIN` (15) percben. Az ablak végén tárcánként elszámolja a PnL-t: kapott SOL + a maradék token értéke az utolsó áron − befizetett SOL. A dev és a 2 mp-en belüli (bundle, gyors sniper) vevők kimaradnak, mert őket úgysem lehet követni. Csak a lezárt ablakok számítanak, így nincs előrelátás.
- `S6_okos` / `S6_okos2`: belépés 1 mp késéssel, ha egy, illetve legalább két „okos” tárca vásárol. Okos tárca: legalább 5 token, legalább 50% nyerő arány, összesen legalább +0,5 SOL; mindhárom állítható.
- `S7_organikus`: 60 mp-nél belép, ha legalább 15 külön vevő van, a korai vevők a kínálat kevesebb mint 15%-át vették, a dev nem adott el, és az eladások aránya 0,6 alatt van.
- A pump.fun görbe-díj alapértéke 1% → **1,25%** (2026: 0,95% protokoll + 0,30% készítő).
- A `diag` és a Telegram-jelentés kiírja a tárca-adatbázis méretét, az okos tárcák számát és a kereskedés-üzenetek számát, és figyelmeztet, ha nem jön kereskedés-üzenet.

## Telegram: /export és /version

- `/export`: egy zip a sniper és a carry adatfájljaival (`state/paper_sniper_trades.csv`, `paper_sniper_state.json`, `paper_carry_log.csv`, `paper_carry_state.json`), valamint a `report`, a `report --kiszallas`, a `diag` és a carry `report` kimenetével. Csak a felsorolt fájlok kerülnek bele (a `.env` soha), a szövegekből a titkok ki vannak takarva.
- `/version`: a futó commit, a telepítetlen commitok és PR-ek száma az `origin/main`-hez képest, a VPS helyi commitjai, és melyik szolgáltatás fut a lemezen lévőnél régebbi kóddal (újraindítás kell).
- A 2 órás jelentés első részében egy sor jelzi a telepítetlen PR-eket és az újraindításra váró szolgáltatásokat.

## Paper sniper: párhuzamos kiszállási változatok, párosított összevetés

- Minden szimulált belépést 6 kiszállási szabály szerint is lezárunk: `alap` (konfig TP/SL/időkorlát), `trail`, `gyors`, `reszl` (részleges TP + trail / nullszint), `szoros`, `futtat`. A CSV-ben `S2_gyors~trail` formában szerepelnek. Az alap sorok neve nem változott, így a korábbi adatokkal folytonos.
- `report --kiszallas`: a változatok párosított különbsége az alap-hoz képest (azonos belépések), 95%-os hibahatárral és JOBB/ROSSZABB ítélettel, stratégiánként is. A `report`, a `diag` és a Telegram-jelentés összesítői az alap kiszállással számolnak, a változatok külön blokkban jelennek meg.
- `PAPER_SNIPER_EXITS` a változatok szűréséhez. A `PAPER_SNIPER_MAX_OPEN` alapértéke 60 → 120, mert a leghosszabb változat 60 percig tart.

## Telegram-vezérlő a papír-botokhoz, 2 órás jelentés

- A `scripts/telegram_control_bot.py` a leállított grid bot helyett a paper snipert és a paper carryt kezeli. A grid bot parancsai törölve: `/orders`, `/position`, `/trades`, `/performance`, `/cancelall`, `/panic`, `/closeposition`, `/startbot`, `/stopbot`, `/restartbot`, `/update`. Induláskor a `setMyCommands` a Telegram-menüt is lecseréli.
- Rendszeres jelentés `TELEGRAM_REPORT_INTERVAL_HOURS` óránként (alap: 2). Tartalma: szolgáltatások állapota, lemez és memória; a sniper elmúlt ablaka (új token/óra, graduáció, metaadat-arány, lezárt pozíciók, legjobb trade) és stratégiánkénti összesítő CI-vel és ítélettel; a carry equity, az ablakbeli funding és díj, a pozíció, a likvidálási távolság és a piaci funding; figyelmeztetések.
- Riasztás, ha a sniper vagy a carry szolgáltatás leáll vagy újraindul.
- Új parancsok: `/report`, `/status`, `/sniper`, `/diag`, `/carry`, `/logs_*`, `/restart_*`, `/stop_*` (megerősítéssel), `/start_*`, `/pull` (`git pull --rebase`, hiba esetén `rebase --abort`), `/menu`.
- `paper_sniper report --telegram --ora N` és `paper_carry telegram --ora N`: tömör jelentés. A régi napi Telegram-összesítők megszűntek.

## Re-peg the maker-first flatten so defensive exits stop escalating to taker

- Trade-history analysis of the May 28–Jun 23 export (676 fills, reconciled to the exchange net of −$5.97) confirmed the edge breakdown again: gross was ~flat (−$0.65) and **fees were the loss**, concentrated in ~25 taker defensive exits. ~**$1.51** of that was pure taker fee a maker exit would have avoided, and a separate ~−$3.57 came from the Jun 11/14 over-size churn burst (already prevented by the `_submit_live_limit` per-trade/per-position caps). See `docs/trade_history_loss_analysis.md`.
- Root cause of the taker leak: the maker-first flatten placed its post-only reduce-only order **once** at the passive touch and, if the mark walked away during the 8s window (exactly what trend-move exits do), it escalated to a taker cross. The maker attempt only paid off in calm markets.
- **Fix: re-peg while the window is open.** `_maybe_repeg_flatten` keeps the resting reduce-only exit pinned to the passive touch as the mark drifts (cancel/replace, still post-only), so the exit keeps filling at the maker fee instead of being left behind. The taker cross stays as the backstop once the window expires; the deadline is preserved across re-pegs.
- New knob `FLATTEN_MAKER_REPEG_PCT` (live default **0.0006**, re-peg on ≥0.06% drift; **0.0** disables and preserves the original one-shot behaviour). `FLATTEN_MAKER_WAIT_SECONDS` 8 → **20** to give the maker order more time to fill before crossing. Backward-compatible defaults; covered by `test_live_flatten_maker_repeg_tracks_touch_then_escalates`.

## Crash-exit machinery: maker-first flatten + a wide catastrophe backstop

- Added config-driven crash handling that keeps the data-following posture (live data shows tight taker stops lose money, so nothing here tightens the existing 1.5% defensive exits):
  - **Maker-first flatten** (`FLATTEN_MAKER_FIRST=true`, `FLATTEN_MAKER_WAIT_SECONDS=8`): every forced exit now first rests a post-only reduce-only order on our own side of the book (maker fee) instead of crossing as taker. If it has not filled by the deadline it escalates to an aggressive taker cross. This makes *all* defensive exits — adverse-move, wrong-way, hard stop — cheaper without making them fire any sooner.
  - **Hard position stop** (`POSITION_STOP_LOSS_PCT`, default **0.025**): a regime- and mean-reversion-independent catastrophe backstop that flattens any position 2.5% underwater no matter what. Deliberately wide — it only catches an unbounded run, not normal grid drawdown. Set to 0 to disable. Runs before every other exit.
  - **Mean-reversion override** (`MEAN_REVERSION_OVERRIDE_PCT`, default **0.0** = off): once an adverse move reaches this size, the "wait for the one-bar bounce" deferral on the adverse-move exit is ignored and the exit fires anyway, so a small bounce can no longer defer an exit indefinitely.
- All knobs are config-driven with backward-compatible defaults; `ADVERSE_MOVE_EXIT_PCT` and `WRONG_WAY_EXIT_LOSS_PCT` stay at 0.015. The aggressive spec values (0.5% / 1.0% stops) are deliberately **not** deployed — the machinery is in place to tighten later only if the edge breakdown justifies it.

## Widen the defensive taker exits — they were the whole loss

- The corrected edge breakdown made it decisive: the maker grid is **profitable** (+$2.35 over the bot fills, manual deficit-bursts excluded), and the **entire** loss comes from **74 defensive taker closes (−$4.67)** — split Close Short −$2.48 / Close Long −$2.19. These per-position exits cross the book as taker (3x fee) and close at a loss, fighting the grid's mean-reversion.
- Widened both exits accordingly: `ADVERSE_MOVE_EXIT_PCT` 0.008 → **0.015**, and the wrong-way exit (previously hard-coded at a very tight 0.0035) is now configurable as `WRONG_WAY_EXIT_LOSS_PCT`, default **0.015**. The grid now holds through normal adverse moves and only force-exits a genuinely large one. The remaining trend protection is the max-position cap, the market-stress layer (one-siding / flat), and 1x leverage (liquidation effectively impossible). Goal: keep the +$2.35 maker edge instead of giving it back through taker stops.

## systemd unit for the Telegram control bot

- Added `deploy/systemd/hyperliquid-telegram-control.service` so the Telegram controller runs as a managed service instead of a manual background process. Previously it was launched by hand and never restarted, so it kept serving stale code after a `git pull` (the `/performance` output lagged the deployed version) and would not survive a reboot. As a service it auto-restarts on failure/reboot; after a code update, `systemctl restart hyperliquid-telegram-control` reloads it.

## /performance edge breakdown: maker vs taker and by regime

- `/performance` now appends an EDGE BONTÁS section so it is visible from the phone where the edge comes from and where it leaks: net PnL (realized − fee) split by maker vs taker liquidity, the maker share (target ≥90%), per-regime net (RANGE / TREND_*), and a rolling 24h net. This turns the "is the strategy actually profitable" question into a glanceable answer (the maker-profit vs taker-loss split that previously needed manual CSV analysis) — the gate before any scaling decision.

## Pair a take-profit with the residual a flip leaves behind

- A regime flip (`Short>Long` / `Long>Short`) closes the old side and opens a small residual in the new direction (observed live: a 0.00044 buy closing a 0.00033 short left a ~$7 long). That residual got no paired take-profit (only plain opening fills did), so it lingered with no close order and tended to be flattened later as a taker loss. `_maybe_place_paired_take_profit` now also handles flip fills, sizing the reduce-only TP to the residual (`position_after`), not the whole flip size. Plain opening/increasing fills are unchanged.

## Widen the adverse-move exit so the grid stops whipsawing

- Live data showed the maker take-profits earning roughly +$0.90 while the defensive taker exits gave back about −$0.79 (net ~breakeven). The adverse-move exit at 0.45% (~1.5x the grid spacing) fired on normal oscillation and realized taker losses on positions that would have mean-reverted into their maker TP. Raised `ADVERSE_MOVE_EXIT_PCT` 0.0045 → 0.008 (~3x grid spacing): the grid holds through normal chop and only taker-exits a genuinely larger adverse move. At a $90 max position a 0.8% exit is ~$0.72 (≈0.2% of $325 equity), well inside the 1.5% daily limit; the real trend backstop remains the max-position cap plus the market-stress layer.

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
