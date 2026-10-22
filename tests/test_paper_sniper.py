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
    for name in ("STATE_FILE", "DB_FILE", "TRADES_FILE", "TOKEN_LOG", "MIGR_LOG", "POSTGRAD_LOG", "WALLET_LOG", "STOP_FILE", "LOCK_FILE"):
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
    assert cases
    for case in cases:
        assert case["line"].startswith("Program data: vdt/007mYe")      # TradeEvent discriminator base64-ben
        ev = mod.decode_trade_event(case["line"][len("Program data: "):])
        exp = case["expected"]
        assert ev is not None, case["source"]
        for k in ("txType", "mint", "traderPublicKey", "ts"):
            assert ev[k] == exp[k], k
        for k in ("solAmount", "tokenAmount", "vSolInBondingCurve", "vTokensInBondingCurve"):
            assert ev[k] == pytest.approx(exp[k], rel=1e-9), k
        # fuggetlen ellenorzes az eltolasokra: a gorbe szorzata allando, a valodi SOL = virtualis - 30
        assert ev["vSolInBondingCurve"] * ev["vTokensInBondingCurve"] == pytest.approx(mod.K_CURVE, rel=1e-3)
        raw = base64.b64decode(case["line"][len("Program data: "):])
        real_sol, = struct.unpack_from("<Q", raw, 113)
        assert real_sol / 1e9 == pytest.approx(ev["vSolInBondingCurve"] - mod.V0_SOL, abs=1e-6)
        assert real_sol / 1e9 == pytest.approx(exp["realSol"], abs=1e-6)


def test_decode_trade_event_rejects_other_events(tmp_path, monkeypatch):
    mod = load_sniper(tmp_path, monkeypatch)
    create = mod.CREATE_EVENT_DISC + b"\x00" * 200
    assert mod.decode_trade_event(base64.b64encode(create).decode()) is None
    line = json.loads((FIXTURES / "pump_trade_events.json").read_text())[0]["line"][len("Program data: "):]
    assert mod.decode_trade_event(base64.b64encode(base64.b64decode(line)[:100]).decode()) is None   # csonka
    assert mod.decode_trade_event("nem base64!!") is None


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
    p_sl = open_position(mod, S, "S10_bennfentes", "SLmint", 60.0)
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
