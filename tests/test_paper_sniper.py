"""scripts/paper_sniper: dekodolas, gorbe-matek, kiszallasok (poll_once), itelet-kuszob, tokens S14-jel."""
import asyncio
import base64
import csv
import importlib.machinery
import importlib.util
import json
import struct
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_sniper(tmp_path, monkeypatch):
    """a script modulkent, az osszes allapotfajl a tmp_path-ban, kornyezeti konfig nelkul"""
    loader = importlib.machinery.SourceFileLoader("paper_sniper_test", str(ROOT / "scripts" / "paper_sniper"))
    spec = importlib.util.spec_from_loader("paper_sniper_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    for name in ("STATE_FILE", "DB_FILE", "TRADES_FILE", "TOKEN_LOG", "MIGR_LOG", "POSTGRAD_LOG", "WALLET_LOG", "CURVE_LOG",
                 "STOP_FILE", "LOCK_FILE"):
        monkeypatch.setattr(mod, name, tmp_path / getattr(mod, name).name)
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mod, "load_env", lambda: None)
    for k in [k for k in __import__("os").environ if k.startswith("PAPER_SNIPER_")]:
        monkeypatch.delenv(k)
    return mod


def curve_acc(mod, vsol_sol, done=False):
    """pump.fun BondingCurve fiok (base64) a valodi gorbe szorzataval: vsol * vtok = 30 SOL * 1.073e9 token"""
    vsol = int(vsol_sol * 1e9)
    vtok = int(30e9 * 1.073e15 / vsol)
    b = bytes(8) + struct.pack("<QQQQQ", vtok, vsol, max(0, vtok - int(279.9e12)), vsol - int(30e9), int(1e15)) + bytes([int(done)])
    return {"owner": mod.PUMP, "data": [base64.b64encode(b).decode(), "base64"]}


class FakeRpc:
    def __init__(self, mod):
        self.mod, self.curves, self.calls, self.errors = mod, {}, 0, 0

    async def call(self, method, params, retries=3):
        if method == "getAccountInfo":
            return {"value": self.curves.get(params[0])}
        assert method == "getMultipleAccounts"
        return {"value": [self.curves.get(k) for k in params[0]]}


def make_sniper(mod):
    S = mod.Sniper()
    S.rpc = FakeRpc(mod)
    return S


def open_position(mod, S, strat, mint, vsol):
    """valodi belepesi kod (enter) a hamis RPC gorbejen"""
    S.rpc.curves["C" + mint] = curve_acc(mod, vsol)
    T = {"mint": mint, "curve": "C" + mint, "name": mint, "symbol": mint[:4], "t": time.time(), "tr": {}, "leaders": {}}
    asyncio.run(S.enter(strat, T, 0))
    return S.positions[-1]


def closed(mod):
    if not mod.TRADES_FILE.exists():
        return []
    with mod.TRADES_FILE.open() as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------- dekodolas
def test_decode_trade_event_fixture_lines(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    cases = json.loads((FIXTURES / "pump_trade_events.json").read_text())
    assert any(c["source"].startswith("VALODI") for c in cases)            # legalabb egy valodi, lancrol vett sor
    for case in cases:
        assert case["line"].startswith("Program data: vdt/007mYe")      # TradeEvent discriminator base64-ben
        ev = mod.decode_trade_event(case["line"][len("Program data: "):])
        exp = case["expected"]
        assert ev is not None, case["source"]
        for k in ("txType", "mint", "traderPublicKey", "ts"):
            assert ev[k] == exp[k], k
        for k in ("solAmount", "tokenAmount", "vSolInBondingCurve", "vTokensInBondingCurve", "fee"):
            assert ev[k] == pytest.approx(exp[k], rel=1e-9, abs=1e-12), k
        # fuggetlen ellenorzes az eltolasokra: a valodi tartalekok a 113. bajttol; a token-oldal minden gorben
        # a szabvanyos kinalatbol indul (1073M virtualis, 793.1M valodi), ezert a kulonbseg mindig 279.9M token
        raw = base64.b64decode(case["line"][len("Program data: "):])
        real_sol, real_tok = struct.unpack_from("<QQ", raw, 113)
        assert real_sol / 1e9 == pytest.approx(exp["realSol"], abs=1e-9)
        assert real_tok / 1e6 == pytest.approx(exp["realTok"], rel=1e-12)
        assert ev["vTokensInBondingCurve"] - real_tok / 1e6 == pytest.approx(279.9e6, rel=1e-9)
        assert real_sol / 1e9 <= ev["vSolInBondingCurve"]
        if exp.get("standardCurve"):                                     # szabvanyos gorbe: allando szorzat, 30 SOL virtualis
            assert ev["vSolInBondingCurve"] * ev["vTokensInBondingCurve"] == pytest.approx(mod.K_CURVE, rel=1e-3)
            assert real_sol / 1e9 == pytest.approx(ev["vSolInBondingCurve"] - mod.V0_SOL, abs=1e-6)
            assert ev["fee"] == pytest.approx(ev["solAmount"] * 0.0125, rel=0.01)   # 95 + 30 bps
        if exp.get("mayhemMode"):                                        # a mayhem-gorbe NEM a 30 SOL-os (V0_SOL nem ervenyes)
            assert ev["vSolInBondingCurve"] - real_sol / 1e9 < 5
        assert ev["realSol"] == pytest.approx(exp["realSol"], abs=1e-9)
        assert ev["mayhem"] is exp["mayhemMode"]                        # az ix_name string utani bool


def test_decode_trade_event_rejects_other_events(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    create = mod.CREATE_EVENT_DISC + b"\x00" * 200
    assert mod.decode_trade_event(base64.b64encode(create).decode()) is None
    line = json.loads((FIXTURES / "pump_trade_events.json").read_text())[0]["line"][len("Program data: "):]
    assert mod.decode_trade_event(base64.b64encode(base64.b64decode(line)[:100]).decode()) is None   # csonka
    assert mod.decode_trade_event("nem base64!!") is None
    old = mod.decode_trade_event(base64.b64encode(base64.b64decode(line)[:113]).decode())   # regi, rovid elrendezes
    assert old is not None and old["fee"] is None


# ---------------------------------------------------------------- tarca-elszamolas
def tracked_token(mod, S, mint, dev, t):
    """ugyanazok a mezok, mint a track()-ben (call_later nelkul)"""
    T = {"mint": mint, "curve": "C" + mint, "dev": dev, "t": t, "name": mint, "symbol": "X", "done": set(), "copy": False,
         "tr": {}, "buyers": set(), "early_tok": 0.0, "n_buy": 0, "n_sell": 0, "dev_sold": False, "px": None, "smart": set(),
         "pt": [], "pp": [], "leaders": {}, "snaps": {}, "pmax": {}, "buy_sol": 0.0, "max_in": 0.0, "losers": set(),
         "vs": mod.V0_SOL, "cslot": None}
    S.tokens[mint] = S.tracked[mint] = T
    return T


def trade(w, mint, side, sol, tok, vsol, fee):
    return {"txType": side, "mint": mint, "traderPublicKey": w, "solAmount": sol, "tokenAmount": tok,
            "vSolInBondingCurve": vsol, "vTokensInBondingCurve": mod_K / vsol, "fee": fee}


mod_K = 30.0 * 1.073e9


def wallet_rows(mod, w="W" * 40):
    """a tarca naplosorai mint szerint (mas tarcak sora az 5%-os veletlen mintaba eshet)"""
    with mod.WALLET_LOG.open() as f:
        return {r["mint"]: r for r in csv.DictReader(f) if r["wallet"] == w}


def test_wallet_pnl_includes_the_wallets_own_fees(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    w, dev, mint = "W" * 40, "D" * 40, "F" * 40
    S.wallets[w] = [3, 1, 0.0, 0]                                        # visszatero tarca: a naploba kerul
    T = tracked_token(mod, S, mint, dev, time.time() - 2000)
    S.on_trade(trade(w, mint, "buy", 1.0, 3e7, 40.0, 0.0125), T["t"] + 60)
    S.on_trade(trade(w, mint, "sell", 0.6, 1.5e7, 39.0, 0.0075), T["t"] + 90)
    S.finalize_tracked()
    px = 39.0 / (mod_K / 39.0)
    rem = 1.5e7 * px * (1 - S.fee)
    exp = 0.6 + rem - 1.0 - (0.0125 + 0.0075)
    assert float(wallet_rows(mod)[mint]["pnl"]) == pytest.approx(exp, abs=1e-4)
    assert S.wallets[w][2] == pytest.approx(exp, abs=1e-4)
    # dijmezo nelkul (regi esemeny): a konfig szerinti 1.25% / irany
    m2 = "G" * 40
    T2 = tracked_token(mod, S, m2, dev, time.time() - 2000)
    b, sl = trade(w, m2, "buy", 1.0, 3e7, 40.0, None), trade(w, m2, "sell", 0.6, 1.5e7, 39.0, None)
    S.on_trade(b, T2["t"] + 60)
    S.on_trade(sl, T2["t"] + 90)
    S.finalize_tracked()
    assert float(wallet_rows(mod)[m2]["pnl"]) == pytest.approx(0.6 + rem - 1.0 - 1.6 * S.fee, abs=1e-4)


def test_graduated_token_waits_for_pumpswap_price(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    w, dev, mint = "W" * 40, "D" * 40, "H" * 40
    S.wallets[w] = [3, 1, 0.0, 0]
    T = tracked_token(mod, S, mint, dev, time.time() - 2000)
    S.on_trade(trade(w, mint, "buy", 1.0, 2e7, 80.0, 0.0125), T["t"] + 60)
    S.on_trade(trade("V" * 40, mint, "buy", 5.0, 1e7, 115.0, 0.0625), T["t"] + 400)   # a graduacio elotti csucs
    S.on_migrate({"mint": mint})
    assert T["grad_t"] and mint in S.postgrad_due
    S.finalize_tracked()
    assert mint in S.tracked                                             # var a PumpSwap-arra
    curve_px = T["px"]
    S.record_postgrad([mint], {mint: {"px": curve_px * 0.4, "liq": 20000.0}}, time.time())
    assert T["px_amm"] == pytest.approx(curve_px * 0.4)
    S.finalize_tracked()
    assert mint not in S.tracked
    rem = 2e7 * curve_px * 0.4 * (1 - S.amm_fee)                         # PumpSwap-ar es AMM-dij, nem a gorbe csucsa
    assert float(wallet_rows(mod)[mint]["pnl"]) == pytest.approx(rem - 1.0 - 0.0125, abs=1e-4)
    # ha a DexScreener nem adott arat: a probalkozasok utan a gorbe araval elszamolva, nem var orokke
    m2 = "J" * 40
    T2 = tracked_token(mod, S, m2, dev, time.time() - 2000)
    S.on_trade(trade(w, m2, "buy", 1.0, 2e7, 80.0, 0.0125), T2["t"] + 60)
    S.on_migrate({"mint": m2})
    for _ in range(3):
        S.record_postgrad([m2], {}, time.time())
    S.finalize_tracked()
    assert m2 not in S.tracked and m2 in wallet_rows(mod)


def test_s8_retired_and_old_strat_list_still_works(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    assert "S8_masol" not in mod.STRAT_DESC
    monkeypatch.setenv("PAPER_SNIPER_STRATS", "S8_masol,S10_bennfentes,S13_amm,S14_volumen")
    S = make_sniper(mod)
    assert S.strats == ["S10_bennfentes", "S13_amm", "S14_volumen"]
    # egy masolhato tarca vetele mar nem nyit poziciot
    w, mint = "W" * 40, "K" * 40
    S.wallets[w] = [20, 15, 5.0, 0, 20, 18, 4.0, 1.0]
    assert S.is_copyable(w)
    T = tracked_token(mod, S, mint, "D" * 40, time.time())
    S.on_trade(trade(w, mint, "buy", 1.0, 3e7, 40.0, 0.0125), time.time())
    assert T["leaders"] == {} and T["done"] == set()


# ---------------------------------------------------------------- gorbe-matek
def test_buy_sell_round_trip_costs_two_fees(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    fee, lam = 0.0125, 0.1e9
    c0 = mod.parse_curve(curve_acc(mod, 40.0))
    tok = mod.buy_tokens(c0, lam, fee)
    net = lam * (1 - fee)
    # a valodi gorbe a vetelunk utan: ugyanazt a tokent visszaadva a dij utani SOL jon vissza, meg egyszer dijazva
    c1 = {"vsol": c0["vsol"] + net, "vtok": c0["vtok"] - tok, "rsol": c0["rsol"] + net, "done": False}
    ret = mod.sell_sol(c1, tok, fee) / lam - 1
    assert ret == pytest.approx((1 - fee) ** 2 - 1, abs=1e-9)           # = -2 x dij + dij^2
    # papir-elszamolas: a vetelunk nincs a valodi gorbeben -> kis tobbletcsuszas, de kb. -2 x dij
    paper = mod.sell_sol(c0, tok, fee, own_lam=net) / lam - 1
    assert -2 * fee - 0.01 < paper < -2 * fee + fee ** 2
    # friss gorbe (0 valodi SOL): csak a sajat befizetesunk veheto ki
    fresh = mod.parse_curve(curve_acc(mod, 30.0))
    t2 = mod.buy_tokens(fresh, lam, fee)
    assert mod.sell_sol(fresh, t2, fee, own_lam=net) <= net * (1 - fee) + 1e-6
    # a CSV-bol ujraszamolt ertek (diag: 'azonnali vetel+eladas koltsege') ugyanezt adja
    v = mod.recompute_value({"cost_sol": 0.101, "entry_vsol": 40.0, "exit_vsol": 40.0, "graduated": "0"}, fee, 0.001)
    assert v + 0.001 == pytest.approx(mod.sell_sol(c0, tok, fee, own_lam=net) / 1e9, rel=1e-6)


# ---------------------------------------------------------------- kiszallasok
def test_poll_once_alap_tp_sl_and_time(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    p_tp = open_position(mod, S, "S10_bennfentes", "TPmint", 40.0)
    open_position(mod, S, "S10_bennfentes", "SLmint", 60.0)
    p_t = open_position(mod, S, "S10_bennfentes", "IDOmint", 40.0)
    assert set(p_tp["exits"]) == {"alap", "masol"}
    p_t["t_open"] -= 31 * 60                                             # alap: max 30 perc
    S.rpc.curves["CTPmint"] = curve_acc(mod, 60.0)                       # ar ~ vsol^2: 2.25x -> TP (2.0x)
    S.rpc.curves["CSLmint"] = curve_acc(mod, 35.0)                       # ~0.34x -> SL (0.5x)
    asyncio.run(S.poll_once())
    rows = {r["mint"]: r for r in closed(mod)}
    assert rows["TPmint"]["reason"] == "TP" and float(rows["TPmint"]["ret_pct"]) > 100
    assert rows["SLmint"]["reason"] == "SL" and float(rows["SLmint"]["ret_pct"]) < -50
    assert rows["IDOmint"]["reason"] == "ido" and -6 < float(rows["IDOmint"]["ret_pct"]) < -4   # ar valtozas nelkul: a koltseg
    assert all(r["strat"] == "S10_bennfentes" for r in rows.values())   # a masol valtozat vezeto nelkul torolve
    assert S.positions == []


def test_poll_once_s14_trail_gyors_then_alap(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    p = open_position(mod, S, "S14_volumen", "S14mint", 40.0)
    assert set(p["exits"]) == {"alap", "gyors"}
    S.rpc.curves["CS14mint"] = curve_acc(mod, 50.0)                      # ~1.5x: a gyors csucsa 1.3 felett
    asyncio.run(S.poll_once())
    assert closed(mod) == []
    S.rpc.curves["CS14mint"] = curve_acc(mod, 41.0)                      # ~1.03x: csucstol -30% alatt -> gyors trail
    asyncio.run(S.poll_once())
    rows = closed(mod)
    assert [(r["strat"], r["reason"]) for r in rows] == [("S14_volumen~gyors", "trail")]
    S.rpc.curves["CS14mint"] = curve_acc(mod, 33.0)                      # ~0.66x: csucstol -50% alatt -> alap trail
    asyncio.run(S.poll_once())
    rows = closed(mod)
    assert [(r["strat"], r["reason"]) for r in rows][-1] == ("S14_volumen", "trail")
    assert S.positions == []


def test_poll_once_graduation_closes_curve_strategy_and_moves_s14_to_amm(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    assert S.amm_fee == pytest.approx(0.012)                            # PumpSwap friss graduált sav: 1.2% / irany
    p10 = open_position(mod, S, "S10_bennfentes", "G10mint", 60.0)
    p14 = open_position(mod, S, "S14_volumen", "G14mint", 60.0)
    S.rpc.curves["CG10mint"] = curve_acc(mod, 110.0, done=True)
    S.rpc.curves["CG14mint"] = curve_acc(mod, 110.0, done=True)
    asyncio.run(S.poll_once())
    rows = closed(mod)
    assert [(r["mint"], r["reason"], r["graduated"]) for r in rows] == [("G10mint", "graduált", "1")]
    assert p14 in S.positions and p14["amm"] and p10 not in S.positions

    entry_px = 0.1 * (1 - S.fee) / (p14["tokens"] / 1e6)                 # a belepesi SOL/token ar
    pool = {"liq": 50000.0, "q": 400.0, "vol5": 1.0, "buys5": 1.0, "sells5": 1.0, "chg5": 1.0, "dex": "pumpswap"}
    prices = iter([3 * entry_px, 1.2 * entry_px])                        # PumpSwap: 3x, utana a csucs 40%-a
    seen = []

    def fake_dex(mints):
        seen.append(list(mints))
        return {"G14mint": dict(pool, px=next(prices))}
    monkeypatch.setattr(mod, "dex_pairs", fake_dex)
    asyncio.run(S.poll_once())
    assert closed(mod) == rows and seen == [["G14mint"]]                  # 3x: meg nyitva
    S.dex_last = 0.0                                                     # a DexScreener 30 mp-enkent: most ujra
    asyncio.run(S.poll_once())
    out = {r["strat"]: r for r in closed(mod)[1:]}
    assert out["S14_volumen"]["reason"] == "trail+amm" and out["S14_volumen~gyors"]["reason"] == "trail+amm"
    v = p14["tokens"] / 1e6 * 1.2 * entry_px * (1 - 0.012)
    v *= 400.0 / (400.0 + v)                                             # csuszas a pool SOL-tartalekan
    assert float(out["S14_volumen"]["proceeds_sol"]) == pytest.approx(v - S.prio, abs=2e-6)
    assert S.positions == []


# ---------------------------------------------------------------- itelet-kuszob
def test_verdict_needs_50_positions(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    row = lambda r: {"pnl_sol": r / 1000, "ret_pct": r}
    xs = [row(20 + (i % 5)) for i in range(49)]
    assert mod.strat_stats(xs)["verdict"] == "?"
    assert mod.strat_stats(xs + [row(22)])["verdict"] == "+"
    assert mod.strat_stats([row(-20 - (i % 5)) for i in range(50)])["verdict"] == "-"
    pairs = []
    for i in range(49):
        pairs.append({"strat": "S10_bennfentes", "mint": f"m{i}", "ts_open": str(i), "ret_pct": "-10"})
        pairs.append({"strat": "S10_bennfentes~masol", "mint": f"m{i}", "ts_open": str(i), "ret_pct": str(5 + i % 3)})
    assert "-> ?" in "\n".join(mod.exit_compare(pairs))
    pairs += [{"strat": "S10_bennfentes", "mint": "m49", "ts_open": "49", "ret_pct": "-10"},
              {"strat": "S10_bennfentes~masol", "mint": "m49", "ts_open": "49", "ret_pct": "6"}]
    assert "-> JOBB" in "\n".join(mod.exit_compare(pairs))


# ---------------------------------------------------------------- tokens: S14-jel a 60 mp-es artol
def test_tokens_s14_outcome_from_60s_snapshot(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    rows = []
    for i in range(100):
        sig = i % 10 == 0                                                # minden 10. token: >= 25 SOL vasarlas 30 mp-nel
        base = {k: 0 for k in mod.TOKEN_FEATS}
        r30 = dict(base, ts=1000 + i, mint=f"mint{i:03d}", h="30", died=0, buy_sol=30 if sig else 2, px=1.0,
                   max_mult=4.0 if sig else 1.1, final_mult=2.0 if sig else 0.5)
        # 60 mp-nel mar felment az ar: onnan a nyereseg sokkal kisebb
        r60 = dict(base, ts=1000 + i, mint=f"mint{i:03d}", h="60", died=0, buy_sol=40 if sig else 2, px=2.0 if sig else 1.0,
                   max_mult=2.0 if sig else 1.0, final_mult=1.0 if sig else 0.5)
        rows += [r30, r60]
    with mod.TOKEN_LOG.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=mod.TOKEN_FIELDS)
        w.writeheader()
        w.writerows(rows)
    text = mod.tokens_report("30")
    sec = text[text.index("S14-JEL"):]
    lines = [ln for ln in sec.splitlines() if "mp-es ar:" in ln]
    t30 = next(ln for ln in lines if ln.strip().startswith("tanito") and "30 mp-es ar" in ln)
    t60 = next(ln for ln in lines if ln.strip().startswith("tanito") and "60 mp-es ar" in ln)
    assert "   7 / 100.0%" in t30 and "   7 /   0.0%" in t60                # 30 mp-rol 4x (nyero), 60 mp-rol csak 2x
    assert "| 2.00 |" in t30 and "| 1.00 |" in t60                          # vegso szorzo
    e60 = next(ln for ln in lines if ln.strip().startswith("ellenorzo") and "60 mp-es ar" in ln)
    assert "   3 /" in e60
    assert "alapvonal (minden token, 60 mp-es ar): 0.0% (n=70)" in sec


# ---------------------------------------------------------------- strategia-lista, nyitott poziciok
def test_strat_warnings_unknown_missing_and_restart(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    monkeypatch.setenv("PAPER_SNIPER_STRATS", "S8_masol,S10_bennfentes")
    w = mod.strat_warnings({"strats": ["S10_bennfentes"]})
    assert any("ismeretlen" in x and "S8_masol" in x for x in w)
    assert any(x.startswith("nem fut: S13_amm, S14_volumen") for x in w)
    assert not any("ujrainditas" in x for x in w)
    monkeypatch.setenv("PAPER_SNIPER_STRATS", "")                       # ures = mind fut ...
    w = mod.strat_warnings({"strats": ["S10_bennfentes"]})               # ... de a futo peldany meg a regi listaval
    assert any(x.startswith("nem fut: S13_amm, S14_volumen") for x in w) and any("/restart_sniper" in x for x in w)
    assert mod.strat_warnings({"strats": list(mod.STRAT_DESC)}) == []
    assert mod.strat_warnings({}) == []                                  # regi allapotfajl: csak a konfig szamit
    S = make_sniper(mod)
    S.save(db=False)
    assert json.loads(mod.STATE_FILE.read_text())["stats"]["strats"] == list(mod.STRAT_DESC)


def test_reports_show_open_positions_per_strategy(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    pos = [{"strat": "S13_amm", "t_open": time.time()}] * 2 + [{"strat": "S14_volumen", "t_open": time.time()},
                                                                {"strat": "S8_masol", "t_open": time.time()}]
    now = time.time()
    mod.STATE_FILE.write_text(json.dumps({"positions": pos, "saved": now,
                                          "stats": {"started": now - 3600, "strats": ["S10_bennfentes", "S13_amm", "S14_volumen"]}}))
    with mod.TRADES_FILE.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(mod.TRADE_FIELDS)
        for i in range(3):
            w.writerow([round(now) - 600, round(now) - 60, "S14_volumen", f"m{i}", "n", "s", 60, 40, 41, 0.101, 0.11, 0.009, 9, "trail", 540, 0])
    text = mod.report_text()
    assert "S13_amm: meg nincs lezart pozicio, nyitott most 2" in text
    assert "S10_bennfentes: meg nincs lezart pozicio, nyitott most 0" in text   # fut, de meg semmi
    assert "lezart poziciok: 3   nyitott most: 1" in text
    assert "S8_masol 1" in text and "meg lezarulnak" in text
    tg = mod.telegram_report(2)
    assert "S13_amm: meg nincs lezart | nyitott 2" in tg
    assert any(ln.startswith("S14_volumen: 3 |") and "| 1  [" in ln for ln in tg.splitlines())
    assert "Nyitott osszesen: 4 (ebbol kivezetett strategiae, meg lezarulnak: S8_masol 1)" in tg
    monkeypatch.setenv("PAPER_SNIPER_STRATS", "S13_amm")                  # a konfig mar mast mond: ujrainditas kell
    assert "/restart_sniper" in mod.telegram_report(2)


# ---------------------------------------------------------------- gorbe: valodi SOL, mayhem
def test_curve_kind_from_trade_events_and_curve_log(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    assert S.max_open == 600                                             # az S14 alap 24 oraig tart: sok nyitott pozicio
    w, dev = "W" * 40, "D" * 40
    S.wallets[w] = [3, 1, 0.0, 0]
    kinds = {"M" * 40: (2.754909464, 0.093446296, True), "N" * 40: (30.742777365, 0.742777365, False),
             "P" * 40: (12.342642748, 1.316464536, None)}
    for mint, (vs, rs, mh) in kinds.items():
        T = tracked_token(mod, S, mint, dev, time.time() - 2000)
        m = trade(w, mint, "buy", 0.5, 1e6, vs, 0.00625)
        m.update(realSol=rs, mayhem=mh)
        S.on_trade(m, T["t"] + 60)
        assert T["rs"] == rs and T["v0"] == pytest.approx(vs - rs, abs=1e-4)
        assert S.token_features(T)["rsol"] == pytest.approx(rs, abs=1e-3)   # nem a virtualis - 30
        assert T["tr"][w][4] == pytest.approx(rs)                        # a tarca belepesi 'gorbe SOL'-ja is
    S.finalize_tracked()
    codes = mod.load_curve_log()
    assert codes == {"M" * 40: 1, "N" * 40: 0, "P" * 40: 2}             # mayhem, szabvanyos, egyeb (jelzo nelkul)
    assert S.stats["curves"] == {"mayhem": 1, "szabvanyos": 1, "egyeb": 1}
    assert mod.curve_kind(None, None) == "ismeretlen" and mod.curve_kind(30.0, False) == "szabvanyos"


def test_tokens_and_wallets_reports_show_curve_band(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    with mod.CURVE_LOG.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "mint", "v0", "mayhem"])
        for i in range(100):
            w.writerow([1, f"mint{i:03d}", 2.66 if i % 2 else 30.0, int(i % 2 == 1)])
    rows = []
    for i in range(100):
        base = {k: 0 for k in mod.TOKEN_FEATS}
        for h in ("30", "60"):
            rows.append(dict(base, ts=1000 + i, mint=f"mint{i:03d}", h=h, died=0, buy_sol=1, px=1.0,
                             max_mult=4.0 if i % 2 else 1.1, final_mult=1.0))
    with mod.TOKEN_LOG.open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=mod.TOKEN_FIELDS)
        wr.writeheader()
        wr.writerows(rows)
    text = mod.tokens_report("30")
    sec = text[text.index("gorbe (-1 ismeretlen"):].splitlines()
    assert any(ln.strip().startswith("1-2") and "100.0%" in ln for ln in sec[1:4])     # a mayhem-sav: minden nyero
    assert any(ln.strip().startswith("0-1") and "  0.0%" in ln for ln in sec[1:4])      # a szabvanyos: egy sem

    smart = "S" * 40
    db = {"wallets": {smart: [10, 8, 5.0, 0]}}
    mod.DB_FILE.write_text(json.dumps(db))
    with mod.WALLET_LOG.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(mod.WALLET_FIELDS)
        for i in range(10):
            wr.writerow([1000 + i, f"mint{i:03d}", smart, 5, 1, 0, 30, 5, 1.0, 1.2, 0, 0.2, 20, 60, 1, 1, 1, 10, 0])
    wt = mod.wallets_report()
    assert "a token gorbeje" in wt and "mayhem" in wt.split("a token gorbeje")[1].split("\n\n")[0]
    assert "mayhem 50% (n=10)" in wt                                     # a legjobb okos tarca tokenjeinek fele mayhem


# ---------------------------------------------------------------- tuladas: tobbet ad el, mint amennyit a gorben vett
def test_oversold_position_is_excluded_from_wallet_pnl(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    agent, normal, dev, mint = "A" * 40, "W" * 40, "D" * 40, "Q" * 40
    S.wallets[agent] = [3, 3, 9.0, 0]
    S.wallets[normal] = [3, 1, 0.0, 0]
    T = tracked_token(mod, S, mint, dev, time.time() - 2000)
    S.on_trade(trade(agent, mint, "buy", 0.5, 1e6, 40.0, 0.0), T["t"] + 60)
    S.on_trade(trade(agent, mint, "sell", 2.0, 5e6, 38.0, 0.0), T["t"] + 90)    # 4M tokent kivulrol kapott (pl. mayhem-ugynok)
    S.on_trade(trade(normal, mint, "buy", 1.0, 3e6, 39.0, 0.0125), T["t"] + 100)
    S.on_trade(trade(normal, mint, "sell", 0.9, 3e6, 38.0, 0.01125), T["t"] + 120)
    assert T["tr"][agent][12] == pytest.approx(4e6) and T["tr"][normal][12] == 0
    S.finalize_tracked()
    a, n = S.wallets[agent], S.wallets[normal]
    assert a[:3] == [3, 3, 9.0] and a[8] == 1 and a[9] == 0               # a hamis +1.5 SOL nem kerult a PnL-be
    assert n[0] == 4 and n[8] == 0 and n[9] == 1
    assert S.stats["wallet_oversold"] == 1
    assert wallet_rows(mod, agent) == {}                                 # a naploba sem kerul
    assert mint in wallet_rows(mod, normal)


def test_oversold_wallet_is_not_smart_and_reports_list_it(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    S = make_sniper(mod)
    agent, good = "A" * 40, "G" * 40
    S.wallets[agent] = [579, 486, 922.0, 0, 0, 0, 0, 0, 3, 2]            # a regi PnL kivalo, de az uj adatban 3/5 tuladott
    S.wallets[good] = [20, 14, 12.0, 0, 0, 0, 0, 0, 1, 19]
    assert mod.wallet_oversold(S.wallets[agent]) and not mod.wallet_oversold(S.wallets[good])
    assert not S.is_smart(agent) and S.is_smart(good)
    assert not mod.wallet_oversold([579, 486, 922.0, 0])                  # regi rekord: nincs adat, nem zarjuk ki
    mod.DB_FILE.write_text(json.dumps({"wallets": S.wallets}))
    with mod.WALLET_LOG.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(mod.WALLET_FIELDS)
        for i in range(5):
            wr.writerow([1000 + i, f"mint{i:03d}", good, 5, 1, 0, 30, 5, 1.0, 1.2, 0, 0.2, 20, 60, 1, 1, 1, 10, 0])
    text = mod.wallets_report()
    assert "GGGGGG..GGGG" in text and "tuladott 1/20" in text
    assert "kizarva az okosok kozul" in text and "AAAAAA..AAAA  tuladott 3/5" in text


# ---------------------------------------------------------------- /wallets: szerencse vagy tudas, hibahatarral
def _skill_log(mod, second_good, second_rest):
    with mod.WALLET_LOG.open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(mod.WALLET_FIELDS)
        for i in range(20):
            first = 20 if i < 10 else -20                                # az elso fele alapjan: 10 jo, 10 tobbi
            for k in range(10):
                ret = first if k < 5 else (second_good if i < 10 else second_rest)[k % 2]
                wr.writerow([1000 + k, f"m{i}{k}", f"{chr(65 + i)}" * 40, 5, 0, 0, 30, 5, 1.0, 1.0, 0, ret / 100, ret,
                             60, 1, 1, 1, 10, 0])
    return mod.wallets_report()


def test_wallet_skill_verdict_needs_significance(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    same = _skill_log(mod, (10, -10), (10, -10))                         # a masodik fele egyforma: nincs tudas
    assert "atlagos hozam" in same and same.count("nem bizonyitott kulonbseg") == 2
    assert "van kitarto tudas" not in same
    small = _skill_log(mod, (11, -10), (10, -10))                        # +0.4 %-pont, a hibahataron belul (a riport +0.3-as esete)
    line_s = next(ln for ln in small.splitlines() if "-> atlagos hozam" in ln)
    assert "+0.4%-ponttal" in line_s and "nem bizonyitott kulonbseg" in line_s
    diff = _skill_log(mod, (30, 25), (-30, 5))                           # a jok a masodik felben is jobbak
    line_a = next(ln for ln in diff.splitlines() if "-> atlagos hozam" in ln)
    line_w = next(ln for ln in diff.splitlines() if "-> nyero arany" in ln)
    assert "jobbak (statisztikailag)" in line_a and "jobbak (statisztikailag)" in line_w
    assert "100% vs 60%" in line_w
    assert mod.num(float("nan")) == "-" and mod.num(1.234) == "1.2"
