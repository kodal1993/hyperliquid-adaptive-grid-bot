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

### pump.fun sniper (`scripts/paper_sniper`) — papír, utolsó esély
- **Ki fizet nekünk, és miért:** eredetileg senki: mi fizetünk a gyorsabbaknak. Az egyetlen
  hihető forrás az információs előny: követhető okos tárcák (S8_masol) és a bundle-ös tokenek
  elkerülése (S7_organikus).
- **Miért nem viszi el más:** a sebességet a fejlesztői bundle-ök és a közel telepített botok
  már elviszik. Hogy 1 mp késéssel követve marad-e előny, épp ezt mérjük.
- **Költségpadló:** kb. −5% / pozíció (1,25% görbe-díj kétszer + 2 × 0,001 SOL prio 0,1 SOL-on).
- **Eddigi eredmény (2026-10-21, javított elszámolás, 1% díjjal):**
  - S0 −6,5%, S2 −9,4% / pozíció, 1000+ pozíción, statisztikailag negatív;
  - díjak előtt nagyjából nulla;
  - a késés nem számít.
- **Bennfentes-klaszter szűrő (2026-10-21-től):** a láncon látható, összehangolt korai vevők
  (visszatérő csoportok, a fejlesztő „barátai”). Elkerülésre használjuk: S7_tiszta, valamint
  S9_klaszter_nelkul a kontroll S9_klaszteres ellen. Siker, ha a klaszter nélküli csoport a
  hibahatárral együtt jobb a klaszteresnél; ha nem, a szűrő nem ér semmit.
- **Mérés:** Telegram `/report`, `/wallets` (4. pont: követhetőség), `/diag` (bennfentes-klaszterek),
  `report --kiszallas`, `/export`.
- **Siker (határidőig):** az S8_masol vagy az S7_organikus legalább 100 pozíción a hibahatárral
  együtt pozitív, 1,25%-os díjjal. Ezután egy második, friss 3 napos időszakon is.
- **Leállítás:** a határidőre nincs másolható tárca, vagy az S6/S7/S8 egyike sem pozitív →
  `systemctl stop hyperliquid-paper-sniper`, a kártya „lezárva: kudarc”.
- **Határidő:** 2026-10-24.

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
