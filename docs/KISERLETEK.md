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

### pump.fun sniper (`scripts/paper_sniper`) — papír: graduáció utáni stratégiák
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
  - A követhető tárcák: 1 mp késéssel, a mi díjainkkal a „okos” tárcák követve −0,8%, minden tárca −7,1%,
    a 93 legalább 8 pozíciós tárca közül egyik sem másolható (2026-10-21). A tárcakövetés nem profitforrás.
  - 2026-10-22: **S8_masol** (másolható tárca követése). 38 144 követő-pozíció után átlag −12,5%, nyerő 24%
    (költségpadló −4,5%); az 569 legalább 8 pozíciós tárca közül egyik sem másolható (az alsó hibahatár sehol
    nem pozitív). Az „okos” tárcák követve −2,8%, a többi −12,1%. A tárcák tudása valódi (a 3. pont szerint a
    második felükben is 18,5 százalékponttal jobbak), de az előnyük a sebesség: a legjobb (BwWK17, 455 token)
    saját maga 84%-ban nyer, követve 15%-ban, −3,4%-kal; medián 25 mp-nél vesz, 69 mp-en belül elad. A mérés
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
- **Aktív** (`PAPER_SNIPER_STRATS=S10_bennfentes,S13_amm,S14_volumen`; a régi listában maradt `S8_masol`-t a
  sniper figyelmen kívül hagyja):
  - `S13_amm` (ténylegesen 2026-10-22 ~10:31-től): belépés graduáció után 5 perccel, ha a DexScreener szerint van tapadás (likviditás
    ≥ 8000 $, 5 perces forgalom ≥ 3000 $, vevők ≥ 1,2 × eladók, emelkedő ár). Késés-független és
    ingyenes: csak a PumpPortal graduációs eseménye és a DexScreener kell. Kiszállás a csúcstól −50%-nál,
    legfeljebb 24 óra, felső célár nélkül.
  - `S14_volumen` (ténylegesen 2026-10-22 ~10:31-től): a token-napló első, ellenőrzött jele. Ha 30 mp-nél legalább 25 SOL a
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
- **Leállítás:** a határidőre egyik sem pozitív → `systemctl stop hyperliquid-paper-sniper`.
- **Határidő:** 2026-11-04.

### HYPE funding carry (`scripts/paper_carry`) — papír
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
