# Kísérleti keret

Hogyan döntjük el, mibe érdemes időt és pénzt tenni. A cél: kevés, jól megindokolt kísérlet,
előre rögzített siker- és leállítási feltételekkel, nem a „gyűjtsünk mindent, és majd kiderül”.

## Szabályok

1. **Szűrés kód előtt.** Három kérdésre kell válasz:
   - **Ki fizet nekünk, és miért?** (pl. funding: a perp longok; LP: a kereskedők díja)
   - **Miért nem vitte el ezt valaki gyorsabb vagy nagyobb?**
   - **Mekkora a költségpadló?** Díj, prioritási díj, slippage, árváltozás nélkül.

   Ha valamelyikre nincs jó válasz, nem építjük meg.
2. **Papírteszt előre rögzített feltételekkel.** A határidő, a siker és a leállítás az indulás
   előtt kerül ide, és utólag nem módosítjuk. A paraméterek hangolása addig, amíg jó nem lesz,
   tilos: egy hangolás = új kísérlet, új határidővel.
   **Korai leállítás** (2026-10-22-től): ha egy stratégia legalább 50 lezárt pozíción a hibahatárral együtt is
   negatív (a riport ítélete `NEGATIV`), a határidő előtt leállítható. Ez csak a veszteseket zárja ki korábban,
   a pozitív irányba nem torzít. Korai *siker* nincs: a pozitívat a határidőig és egy friss időszakon is igazolni kell.
   **Kódstop** (2026-10-22-től): futó kísérlet alatt csak a mérést elrontó hibát javítjuk; új jel, stratégia vagy
   riport-funkció csak új kísérletként, a kapukérdések után.
3. **Mérce:** a HLP vault (2026 október: kb. 3,5% APR). Ami ennél nem jobb a kockázatához mérten,
   annál egyszerűbb a HLP.
4. **Statisztika:**
   - Pozitív = az átlag a 95%-os hibahatárral együtt is pozitív, legalább 100 pozíción.
   - Sok változat közül a legjobb nem bizonyíték. A győztest egy **második, friss időszakon**
     is igazolni kell, mielőtt éles szóba jön.
5. **Legfeljebb 3 aktív kísérlet** egyszerre. Új csak akkor indul, ha egy lezárult.
6. **Éles pénz** csak két igazolt papír-időszak után, a legkisebb méretben, külön döntéssel.

## Kártyasablon

```
### <név> — <állapot: ötlet / papír / lezárva: siker / lezárva: kudarc>
- Ki fizet nekünk, és miért:
- Miért nem viszi el más:
- Költségpadló:
- Mérés: (parancs / Telegram)
- Siker (határidőig):
- Leállítás:
- Határidő:
- Eredmény: (lezáráskor, dátummal)
```

## Kísérletek

### pump.fun sniper (`scripts/paper_sniper`) — lezárva: kudarc (2026-10-23)
- **Ki fizet nekünk, és miért:** a sebességben nem versenyzünk. Hihető forrás a nagy nyerők
  megtartása és a graduáció utáni tapadás, ahol nem a blokk-0 bundle-ökkel versenyzünk. A díj ott sem
  sokkal kisebb: a PumpSwap a friss graduált tokenek sávjában kb. 1,2%-ot számol irányonként
  (nem 0,30%-ot, ahogy eddig számoltunk).
- **Költségpadló:** görbén kb. −5% / pozíció; graduáció után kb. −4,4% (2 × 1,2% + 2 × 0,001 SOL prio
  0,1 SOL-on), plusz a pool méretéből számolt csúszás.
- **AMM-díj javítva (2026-10-22):** a `PAPER_SNIPER_AMM_FEE_BPS` alapértéke 30 → 120. 0,30%-os díjjal számolt S13 vagy
  S14 sor nincs: egyik sem futott a javítás (#126) előtt (lásd lent).
- **Futási idő:** az S13 és az S14 **2026-10-22 10:25-ig egyszer sem futott.** A `/diag` szerint mindkettőnél 0 a belépés,
  és az S13 egyetlen graduációt sem értékelt („S13 kihagyva” sor sincs). Az ok: a futó sniper `PAPER_SNIPER_STRATS` listája
  kizárta őket. **Az S13 és az S14 ténylegesen 2026-10-22 ~10:31-től fut** (a #127 telepítése, a javított listával
  történő újraindítás); az eredményeik ettől az időponttól számítanak. A sniper azóta (#127) induláskor, a `/diag`-ban és
  a Telegram-jelentésben figyelmeztet, ha a lista ismeretlen nevet tartalmaz, egy stratégiát kizár, vagy a konfig mást
  futtatna, mint a futó példány.
  - Első működési adat (2026-10-22 12:21, a `/diag` szerint): S14 61 belépés (36 nyitott, 25 zárt: 24 `trail`, 1
    `trail+amm`); S13 4 belépés 69 értékelt graduációból (kihagyva: kicsi likviditás 31, több eladó 13, kicsi forgalom 8,
    klaszteres vagy másolat 6, eső ár 6, nincs pár 1), 3 zárt (`trail+amm`). Eredmény még nincs: ítélet 50 pozíciótól.
- **Mayhem-tokenek (2026-10-22):** a pump.fun mayhem-módú tokenjeinek görbéje nem a szabványos 30 SOL-os: valódi
  mintákban 2,66 és 11 SOL induló virtuális SOL-t láttunk. A három óránkénti `/diag`-mintából kettő a **BwWK17** (az
  adatbázis „legjobb okos” tárcája: 559 token, 85% nyerő) díjmentes eladása ilyen görbén. Valószínűleg a pump.fun
  mayhem-ügynöke, nem tanulható kereskedő.
  - A token-napló „SOL a görbében” jele és a `/wallets` „görbe SOL” oszlopa ezeken eddig hibás volt (virtuális − 30).
    Mostantól az esemény saját valódi-SOL mezőjéből jön.
  - Tokenenként rögzítve a görbe induló SOL-ja és a mayhem-jelző (`state/paper_sniper_curves.csv`). A `/tokens` és a
    `/wallets` külön sávban mutatja a szabványos, a mayhem és az egyéb görbéket, a `/sniper` stratégiánként görbe
    szerint is bont, a `/wallets` 5. pontja pedig tárcánként mutatja a mayhem-tokenek arányát (a BwWK17-feltevés próbája).
  - A szimulált kereskedéseinket ez nem érinti, mert azok a valódi görbe-fiókot olvassák.
  - **Túladás (2026-10-22):** a BwWK17 PnL-je 12:21 és 14:03 között +704-ről +922 SOL-ra nőtt, és a négy óránkénti mintából
    három az ő díjmentes mayhem-eladása. Feltevés: olyan tokent ad el, amelyet nem a görbén vett, és az elszámolás ezt tiszta
    nyereségnek látta. Mostantól az a tárca-pozíció, amelyben a tárca több tokent adott el, mint amennyit a görbén vett
    (kívülről kapott token: mayhem-kínálat, átutalás, vagy az RPC-kiesés miatt nem látott vétel), kimarad a PnL-ből, a
    naplóból és a követés-mérésből, és tárcánként számolódik. Aki az új adatokban legalább 3-szor és a pozíciói legalább
    20%-ában ilyen, az nem lehet okos; a `/wallets` és a `/diag` külön listázza. A korábbi adatbázis-rekordok PnL-je ettől
    még a régi, hamis érték.
- **Lezárt mérések:**
  - 2026-10-21, kivezetve: S0–S5, S7, S9 és a kiszállási változatok; díjakkal statisztikailag negatív.
  - 2026-10-21: **S6_okos** (−25,2% ±9,1, n=115) és **S6_okos2** (−20,2% ±14,6, n=51) statisztikailag
    negatív: a saját PnL szerint okos tárcák késve követve veszítenek. **Lezárva: kudarc.**
  - A `masol` kiszállás +10,2 ±7,3 százalékponttal jobb az alapnál (n=172), de átlagban még így is
    −11,4%. Tanulság: tárcakövetésnél a vezető eladására kell kiszállni. (Kivétel lehet az S10: ott a
    `masol` 33 százalékponttal rosszabb, n=13, mert a bennfentesek első eladása többnyire csak részeladás.)
  - 2026-10-22: **S12_futtat** −55,4% ±15,8 (n=10, 10-ből 9 veszteséges). A görbe legalább 50%-ánál a
    graduáció előtti csúcsba lép be, utána jön a zuhanás. A token-napló is ezt mutatja. **Lezárva: kudarc**,
    a kódból kivezetve, ahogy az S6 is.
  - 2026-10-22 18:04: **S14_volumen** −35,9% ±10,7 (n=61, 15% nyerő, −2,21 SOL), ítélet `NEGATIV`. A párosított
    `gyors` kiszállás +8,8 ±7,6 százalékponttal jobb az alapnál, de önmagában is −27,1%. A token-napló 30 mp-es jele
    (legalább 25 SOL vásárlás) a mi kb. 60 mp-es belépésünkkel nem fizet: a felfutás tetején veszünk, utána jön a
    zuhanás (a 40 első zárásból 38 a csúcstól −50%-kal). **Lezárva: kudarc**, korai leállítással (2. szabály), a
    `PAPER_SNIPER_STRATS`-ból kivéve. Végső szám a nyitott pozíciók lezárulása után (2026-10-23): −24,9% ±4,6
    (n=161, 6% nyerő, −4,05 SOL); a `gyors` kiszállás +5,4 ±3,3 százalékponttal jobb, de az is −19,5%.
  - 2026-10-23 19:04: **S10_bennfentes** −14,2% ±8,8 (n=90, 9% nyerő, −1,29 SOL), ítélet `NEGATIV`. A klaszter-felismerés
    működik (a tokenek kb. 5%-a klaszteres), de a bennfentesek kb. 40%-ánál már eladtak, mire beléptünk volna, a
    többinél pedig a késéssel a felfutás után veszünk. A `masol` kiszállás +6,4 ±8,7 (nem eldöntött). A valós idejű
    adat (fizetős PumpPortal-kulcs) feltétele, legalább 50 pozíción pozitív eredmény, nem teljesült. **Lezárva: kudarc.**
  - 2026-10-23 19:04: **S13_amm** −54,5% ±10,7 (n=99, 12% nyerő, −5,45 SOL), ítélet `NEGATIV`. A graduáció után 5
    perccel, „tapadás” esetén belépve is a graduáció utáni zuhanásba veszünk; a 1,2%-os AMM-díj is ellene dolgozik.
    **Lezárva: kudarc.**
  - A követhető tárcák: 1 mp késéssel, a mi díjainkkal a „okos” tárcák követve −0,8%, minden tárca −7,1%,
    a 93 legalább 8 pozíciós tárca közül egyik sem másolható (2026-10-21). A tárcakövetés nem profitforrás.
  - 2026-10-22: **S8_masol** (másolható tárca követése). 38 144 követő-pozíció után átlag −12,5%, nyerő 24%
    (költségpadló −4,5%); az 569 legalább 8 pozíciós tárca közül egyik sem másolható (az alsó hibahatár sehol
    nem pozitív). Az „okos” tárcák követve −2,8%, a többi −12,1%. A „jó” tárcák a második felükben kezdetben
    18,5 százalékponttal jobbnak tűntek, de ezt a mayhem-ügynök hamis PnL-je fújta fel. A túladás-szűrés után
    (2026-10-22 este) az átlagos hozamban nincs bizonyított különbség (+0,3 ±7 százalékpont), a nyerő arányban van
    (50% vs 38%): gyakrabban nyernek, de átlagban nem keresnek többet. A „legjobb” tárca (BwWK17) a mayhem-ügynök volt
    (lásd fent); saját maga 84%-ban „nyert”, követve 15%-ban, −3,4%-kal. A mérés
    1 mp késéssel számolt, a valódi késésünk 14–55 mp. **Lezárva: kudarc**, a kódból kivezetve. A követő-mérés
    (`/wallets` 4. pont) és a tárca-adatbázis marad: jelforrás az S10-nek és a token-naplónak.
  - Ugyanekkor javítva a tárca-elszámolás két optimista torzítása: a tárca saját díjai (a TradeEvent `fee` +
    `creator_fee` mezője) eddig kimaradtak, és az ablakban graduált tokenek maradékát a görbe utolsó, csúcs-árán
    értékeltük. Mostantól a PumpSwap-áron 15 perccel a graduáció után. A korábbi adatbázis-rekordok és naplósorok
    optimisták; a fenti következtetést ez nem változtatja meg, mert mindkét torzítás a tárcákat szépítette.
- **Fut, késleltetett adattal: `S10_bennfentes`.** A klaszter-felismerés slot alapú, ezért a késés nem
  rontja el. A bennfentesek eladását viszont kb. 33 mp-cel később látjuk, így az eredmény pesszimista.
  - Első adat (2026-10-21): +34,3% ±72,4, n=6 (egy +182%-os nyerő viszi); ez még nem bizonyíték.
  - Ha legalább 50 pozíción a hibahatárral együtt is pozitív → pontos teszt valós idejű adattal: PumpPortal-kulcs,
    csak a klaszteres tokenekre szűkítve, kb. 0,15 SOL/nap.
- **Aktív** (`PAPER_SNIPER_STRATS=S10_bennfentes,S13_amm`; az S14 2026-10-22 18:04-től kivéve; a régi listában
  maradt `S8_masol`-t a sniper figyelmen kívül hagyja):
  - `S13_amm` (ténylegesen 2026-10-22 ~10:31-től): belépés graduáció után 5 perccel, ha a DexScreener szerint van tapadás (likviditás
    ≥ 8000 $, 5 perces forgalom ≥ 3000 $, vevők ≥ 1,2 × eladók, emelkedő ár). Késés-független és
    ingyenes: csak a PumpPortal graduációs eseménye és a DexScreener kell. Kiszállás a csúcstól −50%-nál,
    legfeljebb 24 óra, felső célár nélkül.
  - `S14_volumen` (2026-10-22 ~10:31 – 18:04, lezárva: kudarc, lásd fent): a token-napló első, ellenőrzött jele. Ha 30 mp-nél legalább 25 SOL a
    vásárlás, az ellenőrző részen 8,6% a nyerő arány, az alapvonal 1,6% (n=35, 1034 tokenből). A 15 perces
    végső szorzó viszont 0,92x, vagyis felfut, majd visszaesik. Két kiszállás párosítva: `alap` (csúcstól −50%,
    24 óra, graduáció után is) és `gyors` (1,3x felett csúcstól −30%, SL 0,6x, 30 perc). Kevés az
    ellenőrző nyerő (kb. 5), ezért a jel még bizonytalan.
  - A nyitott pozíciók plafonja (`PAPER_SNIPER_MAX_OPEN`) 120 → 600 (2026-10-22). Az S14 kb. 35 belépés/óra mellett az
    alap kiszállás akár 24 óráig nyitva tart; 120-nál a jelek egy része „tele volt” címkével kimaradt volna. Ez nem
    stratégiahangolás: a belépési és kiszállási szabály nem változik, csak minden jel bekerül a mintába.
  - A jel 30 mp-nél jön, de a belépés a késés miatt kb. 60 mp-nél történik. A `/tokens` ezért az S14-jel
    kimenetelét a 60 mp-es pillanatkép árától is mutatja, ugyanazokra a tokenekre. Ha a „kivehető” ott 1,0
    alatt van, a jel a mi késésünkkel nem fizet.
- **Mérés:** Telegram `/report`; `/diag` (S13 kihagyási okok); `/export`.
- **Siker:** legalább 50 pozíción a hibahatárral együtt pozitív, utána egy második, friss időszak. A riport
  ítélete (`POZITIV` / `NEGATIV`, kiszállás-összevetésnél `JOBB` / `ROSSZABB`) is csak 50 pozíciótól dönt.
- **Leállítás:** a határidőre egyik sem pozitív → `systemctl stop hyperliquid-paper-sniper`. Egy stratégia korábban is
  leáll, ha legalább 50 lezárt pozíción `NEGATIV` (lásd a 2. szabályt): kivétel a `PAPER_SNIPER_STRATS`-ból
  (`/set`, utána `/restart_sniper`), és ide a lezárt mérések közé kerül az eredménnyel.
- **Ellenőrzési pontok:** 2026-10-22 este (`/report`: az S14 első ítélete), 2026-10-23 este (S10, S13), 10-28 (carry),
  11-04 (sniper határidő).
- **Határidő:** 2026-11-04.
- **Eredmény (2026-10-23): lezárva, kudarc.** Mindhárom futó stratégia statisztikailag negatív: S14 −24,9% ±4,6 (n=161),
  S13 −54,5% ±10,7 (n=99), S10 −14,2% ±8,8 (n=90); összesen 350 pozíció, −10,8 SOL (−30,5% / pozíció) papíron. Előtte
  kudarc volt az S0–S9 és az S12 is. Következtetés: ingyenes, 11–18 mp-cel késő nyilvános adattal (és kb. 5%-os
  körönkénti költséggel) a pump.fun nem profitforrás nekünk. A piac a sebességé (bundle-ök, botok), a bennfenteseké és a
  pump.fun saját mayhem-ügynökéé; a tipikus kereskedő a szabványos görbéken veszít (medián −13,4%). A sniper leállítva;
  ugyanerre a piacra új stratégiát nem írunk. Ami megmarad: a mérőrendszer (díjak, PumpSwap-ár, túladás-szűrés,
  stratégia-figyelmeztetés, tesztek valódi adaton) és ez a napló.

### HYPE funding carry (`scripts/paper_carry`) — lezárva: kudarc, kis különbséggel (2026-10-28)
- **Ki fizet nekünk, és miért:** a HYPE perp longjai fundingot fizetnek a shortoknak. Mi
  delta-semlegesen (spot long + perp short) ezt gyűjtjük.
- **Miért nem viszi el más:** viszik is (vaultok, pl. Neutral Trade), de a funding mindenki
  között megoszlik. Nem verseny, hanem hozam. A kérdés csak az, hogy díjak és bázismozgás után
  jobb-e a HLP-nél.
- **Költségpadló:** nyitás + zárás kb. 2 × (7 bps spot + 4,5 bps perp) + slippage. Kis tőkén
  (50 $ fedezett) ez a funding több napját elviszi.
- **Mérés:** Telegram `/carry`, 2 órás jelentés (equity, funding, bázis, likvidálási távolság).
- **Siker (határidőig):** az évesített nettó hozam a teljes tőkére (100 $) nagyobb, mint a HLP
  APR-ja, likvidálás nélkül.
- **Leállítás:** a nettó hozam a HLP alatt van, vagy a 7 napos funding tartósan a zárási küszöb
  (3%/év) alatt.
- **Határidő:** 2026-10-28.
- **Eredmény (2026-10-28): lezárva, kudarc (kis különbséggel).** 34,9 nap alatt +0,312 $ a 100 $-on, évesítve +3,3%
  (funding +0,362 $, díjak −0,057 $, bázis +0,007 $), likvidálás nélkül; a becsült zárási költséggel (kb. 0,06 $)
  kb. +2,6%/év. A mérce (HLP kb. 3,5%) alatt. A 7 napos funding végig 7–8%/év volt, a zárási küszöb fölött; a
  hozamot a tőkehatékonyság fogta vissza (csak 50 $ fedezett). Tanulság: a funding carry pozitív, de ebben a
  formában csak HLP-szintű; a továbbfejlesztett változat (carry 2.0) a fő eszközökön is csak a HLP körül hozna.
  Megjegyzés: a HLP-mércét 2026 október elején becsültük, a tényleges HLP-hozamot erre az időszakra nem mértük.

### Funding carry 2.0 (`scripts/paper_carry` továbbfejlesztve) — ötlet; indítás a carry 1.0 lezárása (10-28) után, ha a kapu átmegy
- **Ki fizet nekünk, és miért:** a perp longok fundingot fizetnek a shortoknak, mert tőkeáttételt akarnak. A Hyperliquid
  funding-képletében egy fix kamatkomponens is van (ha a prémium kicsi, kb. 11%/év; a kapu ellenőrzi). Mi delta-semlegesen
  (spot long + perp short) ezt gyűjtjük, mint az 1.0-ban.
- **Miért nem viszi el más:** viszik (vaultok, nagy delta-semleges termékek), de ez nem verseny, hanem megosztott hozam.
  A kérdés csak az, hogy díjak után a HLP fölött van-e.
- **Mit tanultunk az 1.0-ból (32,6 nap, HYPE):** a 7 napos funding 7–8%/év volt, a teljes tőkére mégis csak kb. 3,3%/év
  jött, mert (1) a 100 $-ból csak 50 $ volt fedezett pozícióban, (2) taker díjjal léptünk be, (3) a HYPE fundingját
  negatív prémium húzta a kamatkomponens alá.
- **Változások az 1.0-hoz képest (előre rögzítve):**
  1. **Eszközválasztás:** nem fix HYPE, hanem a Hyperliquid-spottal fedezhető eszközök közül az, amelyiknek a 30 napos
     medián fundingja a legmagasabb, és az órák legalább 70%-ában pozitív (`funding_scan`). Eszközváltás legfeljebb
     7 naponként, és csak ha az új 7 napos fundingja legalább 5 százalékponttal magasabb.
  2. **Rezsimkapcsoló:** nyitás, ha a választott eszköz 7 napos fundingja legalább 8%/év; zárás, ha 3%/év alá esik.
  3. **Tőkehatékonyság:** fedezett méret a tőke 65%-a (az 1.0-ban 50%); a perp short kb. 2x tőkeáttétel, és
     automatikus újrasúlyozás (spotból margin a perpbe), ha a likvidálási távolság 60% alá csökken.
  4. **Maker-megbízások** belépéskor és kilépéskor (nem sürgős, várhatunk a teljesülésre): a körönkénti költség kb.
     11 bps a 23 helyett.
- **Költségpadló és várható hozam** (a teljes tőkére, havi egy be+ki körrel, maker díjjal): funding 7,5%/év → kb. 4,0%;
  11%/év → kb. 6,3%; 20%/év → kb. 12%. Vagyis semleges piacon a HLP körül vagy kicsit fölötte, magas fundingnál jóval
  fölötte. Ez nem nagy nyeremény, hanem hozam plusz felkészültség a magas fundingú időszakokra.
- **Kapu (kód előtt):** Telegram `/funding` (a VPS-en: `funding_scan --csak-spot --days 90 --tomor`). Akkor építjük, ha van
  legalább egy Hyperliquid-spottal fedezhető eszköz, amelynek a 90 napos medián fundingja legalább 10%/év, és az órák
  legalább 70%-ában pozitív. Ha nincs, nem építjük: az A (passzív hozam) marad. A fő eszközök a spoton áthidalt tokenként
  léteznek (UBTC, UETH, USOL); ezek is fedezhetők, de a híd a fedezés saját kockázata.
- **Mérés:** papíron, mint az 1.0 (`/carry`, 2 órás jelentés), a teljes tőkére évesített nettó hozammal, a becsült zárási
  költséggel együtt.
- **Siker (határidőig):** a nettó évesített hozam a teljes tőkére legalább a HLP APR + 2 százalékpont, likvidálás nélkül.
- **Leállítás:** 30 nap után a HLP alatt; vagy egy likvidálás; vagy 3 napig nincs a nyitási feltételnek megfelelő eszköz.
- **Határidő:** az indulástól számított 30 nap (pl. 10-29-es indulásnál 11-28).
- **Megfontolt, de most nem jobb alternatívák:**
  - *Passzív carry-termék* (nagy, delta-semleges funding-termékek): ugyanezt a hozamforrást profik futtatják, jobb
    tőkehatékonysággal. Ha a cél csak a pénz, ezt érdemes a B-vel összevetni; kockázata a kibocsátó és a tőzsdék.
  - *Tőzsdék közötti funding-arbitrázs* (Hyperliquid kontra központosított tőzsde): a különbségek néha nagyobbak, de
    KYC, két helyen tartott tőke és átutalási költség kell; 100 $-on a fix költség elviszi. Most elvetve.
  - *Likviditás-szolgáltatás / market making*: lásd a következő kártyát; a tájékozott kereskedők elleni hátrány miatt
    most nem.

### Adatbányászat: mayhem-tokenek és más szeletek (a `scripts/paper_sniper` adatai) — feltárás (2026-10-26)
- **Ki fizet nekünk, és miért:** nyitott kérdés, ezt keressük. A kiinduló megfigyelés: a mayhem-tokeneken (a pump.fun
  ügynöke díj nélkül kereskedik, a görbe nem szabványos) a tárcák 2026-10-22-én pozitívak voltak (medián +9,3%, n=148),
  a szabványos görbéken negatívak (medián −13,4%, n=2205). Ha a dinamika ott más, ott lehet előny.
- **Miért nem viszi el más:** a mayhem-mód új. Ha van hatás, lehet, hogy még nem árazták be; de az is lehet, hogy egyszeri
  zaj volt.
- **Költségpadló:** görbén kb. −5% / pozíció (1,25% díj irányonként + prio). A papírteszt mayhem-tokeneken is 1,25%-kal
  számol (konzervatív: az ügynök díja 0 volt, a miénket nem tudjuk).
- **Szabályok az adatbányászat csapdája ellen** (a feltárás előtt rögzítve):
  1. **Feltáró adat:** ami 2026-10-26-ig összegyűlt. **Megerősítő adat:** csak a jelöltek rögzítése után gyűlő, friss adat.
  2. **Előre felsorolt szeletek** (csak ezek; utólag nem bővítjük):
     - a. `S10_bennfentes` görbe szerint (mayhem / szabványos / egyéb; `/sniper`, „görbe szerint” sor ±CI-vel);
     - b. `S13_amm` ugyanígy;
     - c. `S14_volumen` ugyanígy (a leállításig gyűlt adat);
     - d. token-napló: a görbe-sáv nyerő aránya és a „kivehető” érték (tanító és ellenőrző rész; `/tokens`).
  3. **Jelölt** csak az a szelet lehet, amely a feltáró adaton legalább 30 pozíción az átlag − hibahatár > 0 (a d.-nél:
     a „kivehető” mindkét részen 1,0 fölött, legalább 30 ellenőrző tokennel).
  4. **Megerősítés:** a jelölt(ek) papíron, friss adaton, legalább 50 pozíción. A hibahatár k jelöltnél szigorítva
     (95% helyett 1 − 0,05/k; k = 3-nál kb. 98,3%, vagyis ±2,4 standard hiba ±1,96 helyett).
  5. Ha egy szelet sem jelölt, vagy a megerősítés nem pozitív, az adatbányászat lezárul, kudarc.
- **Adatforrás:** a sniper adatgyűjtésre tovább fut (S10, S13). Egy S14-jelölt megerősítéséhez az S14-et a jelölt szeletre
  szűkítve kellene újraindítani (az új kísérlet része, nem a lezárt S14 folytatása).
- **Mérés:** `/sniper` (görbe szerint, ±CI), `/tokens`.
- **Siker:** legalább egy jelölt a megerősítő adaton a szigorított hibahatárral együtt is pozitív; utána a 6. szabály
  szerint (két papír-időszak, csak utána éles szóba).
- **Leállítás:** nincs jelölt; vagy a határidőre a megerősítés nem pozitív.
- **Határidő:** feltárás 2026-10-27; megerősítés a jelöltek rögzítésétől 14 nap.
- **Eredmény (2026-10-28): lezárva, kudarc (nincs jelölt).** A feltárás (2026-10-26-i adat):
  - a. S10: mayhem −34,4% ±46,0 (n=11, kevés), szabványos −14,1% ±6,3 (n=131);
  - b. S13: szabványos −49,1% ±15,4 (n=40), mayhem-pozíció nincs;
  - c. S14: szabványos −26,8% ±9,1 (n=55), mayhem-pozíció nincs;
  - d. token-napló, mayhem-sáv: az ellenőrző részen 13,4% nyerő (alapvonal 5,1%), „kivehető” 1,12 (n=1402); a tanító
    rész értékét a riport nem mutatja, így a feltétel formálisan nem dönthető el. Kalibráció: ugyanez a mérőszám az
    S14-jelre 1,48-at mutatott, a valódi papír-kereskedés −24,9% lett (n=161); a mayhem-görbék kis tartaléka miatt a
    csúszás is nagyobb. Ezért a d. sem jelölt.
  - A listán kívüli, látványos sávok (pl. dev-részesedés 20% fölött, „kivehető” 3,01) a szabály szerint nem
    jelöltek; ugyanaz a mérőszám áll mögöttük, és a graduációt nyerőnek számolja.

### Likviditás-szolgáltatás (pl. Meteora DLMM / PumpSwap pool) — ötlet, a sniper lezárása után
- **Ki fizet nekünk, és miért:** a kereskedők díjat fizetnek a likviditásért. Itt a „ház”
  oldalán állunk, nem a díjfizetőén.
- **Miért nem viszi el más:** a díj a likviditás arányában oszlik meg. Az előny azoknál a
  pooloknál lehet, ahol a díjbevétel a likviditáshoz képest magas.
- **Költségpadló:** impermanent loss (árfolyameséskor a token nálunk marad) + tranzakciós díjak.
  Memecoinoknál ez lehet nagyobb a díjbevételnél, ezt kell kimérni.
- **Mérés:** még nincs. Papírszimuláció: poolonként díjbevétel kontra impermanent loss.
- **Siker / leállítás / határidő:** az indításkor kerül ide.

### Elvetve (nem indítjuk)
- **Más launchpadok sniperként** (LetsBonk, Meteora DBC stb.): ugyanaz a játék, mint a
  pump.fun, egyre több sniper-ellenes görbével.
- **Arbitrázs poolok között:** MEV-botok uralják. VPS-ről, nyilvános RPC-vel nincs esélyünk
  a sebességben.
