import numpy as np

from jevbot.research.data import load_funding, load_symbol
from jevbot.research.features import add_market_context, compute_symbol_features
from jevbot.research.scanner import CostModel, ScannerConfig, scan
from jevbot.research.synthetic import make_symbol

START = 1_700_006_400_000 - 1_700_006_400_000 % 86_400_000
M = 20 * 1440


def build(tmp_path, events):
    btc = make_symbol(tmp_path, "BTCUSDT", START, M, seed=1, base=60000)
    for i, ev in enumerate(events):
        make_symbol(tmp_path, f"S{i}USDT", START, M, seed=10 + i, btc=btc, beta=1.0, events=ev)
    feats = {}
    for s in ["BTCUSDT"] + [f"S{i}USDT" for i in range(len(events))]:
        b = load_symbol(tmp_path, s, START, START + M * 60_000)
        feats[s] = compute_symbol_features(b, load_funding(tmp_path, s))
    add_market_context(feats)
    return feats


CFG = ScannerConfig(min_qv_24h=0, min_listing_age_d=0)


def test_breakout_detected_with_valid_geometry(tmp_path):
    feats = build(tmp_path, [[(15 * 1440, 0.06, 45)], []])
    props = scan(feats, CFG)
    brk = [p for p in props if p["family"] == "BRK" and p["symbol"] == "S0USDT" and p["side"] == 1]
    t_event = START + 15 * 1440 * 60_000
    near = [p for p in brk if t_event <= p["t_decision"] <= t_event + 60 * 60_000]
    assert near, "injected breakout not detected"
    for p in props:
        s = p["side"]
        assert s * (p["entry_ref"] - p["stop_price"]) > 0 and s * (p["tp_price"] - p["entry_ref"]) > 0
        assert abs(abs(p["tp_price"] - p["entry_ref"]) - p["r_tp"] * p["stop_dist"]) < 1e-9
        assert p["cost_r"] <= CFG.max_cost_r
        assert p["stop_dist"] <= CFG.k_max_atr * p["atr_pct"] * p["entry_ref"] * (1 + 1e-9)


def test_cooldown_and_topk(tmp_path):
    feats = build(tmp_path, [[(15 * 1440, 0.06, 45)]])
    props = scan(feats, CFG)
    by_key: dict = {}
    for p in props:
        by_key.setdefault((p["symbol"], p["family"], p["side"]), []).append(p["tick"])
    for ticks in by_key.values():
        assert all(b - a >= CFG.cooldown_ticks for a, b in zip(ticks, ticks[1:]))
    per_tick: dict = {}
    for p in props:
        per_tick.setdefault(p["tick"], []).append(p)
    for grp in per_tick.values():
        assert sum(p["selected"] for p in grp) <= CFG.top_k


def test_tradable_filter_and_cost_tiers(tmp_path):
    feats = build(tmp_path, [[(15 * 1440, 0.06, 45)]])
    assert not [p for p in scan(feats, CFG, tradable={"BTCUSDT"}) if p["symbol"] == "S0USDT"]
    cm = CostModel()
    assert list(cm.spread_bps(np.array([1, 10, 11, 31]))) == [1.5, 1.5, 3.0, 5.0]
