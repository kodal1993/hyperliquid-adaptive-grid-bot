"""scripts/funding_scan: spot-fedezes (athidalt tokenek), a carry 2.0 kapuja, tomor kimenet."""
import importlib.machinery
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_scan():
    loader = importlib.machinery.SourceFileLoader("funding_scan_test", str(ROOT / "scripts" / "funding_scan"))
    spec = importlib.util.spec_from_loader("funding_scan_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


SPOT_META = {"tokens": [{"name": "USDC", "index": 0}, {"name": "UBTC", "index": 1}, {"name": "HYPE", "index": 2},
                        {"name": "UETH", "index": 3}, {"name": "PURR", "index": 4}, {"name": "USOL", "index": 5}],
             "universe": [{"tokens": [1, 0]}, {"tokens": [2, 0]}, {"tokens": [3, 0]}, {"tokens": [4, 0]}, {"tokens": [5, 2]}]}


def test_spot_hedgeable_maps_bridged_tokens(monkeypatch):
    mod = load_scan()
    monkeypatch.setattr(mod, "post", lambda payload, **k: SPOT_META)
    hedge = mod.spot_hedgeable()
    assert hedge["BTC"] == "UBTC" and hedge["ETH"] == "UETH" and hedge["HYPE"] == "HYPE" and hedge["PURR"] == "PURR"
    assert "SOL" not in hedge                                            # USOL/HYPE nem USDC-s par: nem fedezes


def _rows(mod, monkeypatch, rates, hedge):
    monkeypatch.setattr(mod, "funding_history", lambda coin, days: [rates[coin]] * (24 * days))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    umap = {c: {"vol": 1e7, "oi": 1e7, "funding_now": r} for c, r in rates.items()}
    return mod.analyze(list(rates), 90, 30, umap, hedge, progress=False)


def test_gate_passes_on_a_hedgeable_asset_with_persistent_funding(monkeypatch):
    mod = load_scan()
    hedge = {"BTC": "UBTC", "HYPE": "HYPE"}
    rows = _rows(mod, monkeypatch, {"BTC": 0.0000125, "HYPE": 0.0000085, "SOL": 0.00002}, hedge)   # 10.95 / 7.4 / 17.5 %/ev
    assert [r["coin"] for r in mod.gate(rows)] == ["BTC"]                # a SOL magasabb, de nincs spot-fedezes
    text = mod.compact(rows, 90)
    assert text.startswith("KAPU: ATMENT") and "BTC (UBTC) median 10.9%/ev" in text
    assert any(ln.startswith("  SOL") and ln.rstrip().endswith("| -") for ln in text.splitlines())
    assert any(ln.startswith("* BTC") for ln in text.splitlines())


def test_gate_fails_without_enough_funding(monkeypatch):
    mod = load_scan()
    rows = _rows(mod, monkeypatch, {"HYPE": 0.0000085}, {"HYPE": "HYPE"})
    assert mod.gate(rows) == []
    assert mod.compact(rows, 90).startswith("KAPU: NEM MENT AT")
