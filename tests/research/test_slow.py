from datetime import date, timedelta

import numpy as np

from jevbot.research.a0 import A0Config, run_a0
from jevbot.research.data import DAY, MIN, load_symbol
from jevbot.research.slow import SlowConfig, add_btc_relative, compute_slow_features, scan_slow
from jevbot.research.synthetic import make_symbol

START = 1_704_067_200_000                                    # 2024-01-01
DAYS = 24
LOOSE = SlowConfig(min_qv_24h=0, min_listing_age_d=0)


def _world(hist, n=7):
    btc = make_symbol(hist, "BTCUSDT", START, DAYS * 1440, seed=1, base=60000)
    for i in range(n):
        sign = 1 if i % 2 == 0 else -1
        ev = [(12 * 1440 + 60 * i, 0.12 * sign, 600)]        # multi-hour trend after day 12 ...
        make_symbol(hist, f"S{i}USDT", START, DAYS * 1440, seed=10 + i, btc=btc, beta=1.0, events=ev,
                    drift=sign * 1e-5)                        # ... in the direction of a slow prior drift


def test_slow_features_have_no_lookahead(tmp_path):
    hist = tmp_path / "hist"
    make_symbol(hist, "S0USDT", START, 12 * 1440, seed=4)
    b = load_symbol(hist, "S0USDT", START, START + 12 * 1440 * MIN)
    base = compute_slow_features(b)
    cut = 10 * 1440                                          # poison every minute from day 10 on
    for a in (b.open, b.high, b.low, b.close, b.quote_volume):
        a[cut:] *= 7.0
    poisoned = compute_slow_features(b)
    last_ok = cut // 60 - 1                                  # hourly tick whose bar closes at the cut
    for k, v in base.f.items():
        np.testing.assert_array_equal(v[:last_ok + 1], poisoned.f[k][:last_ok + 1], err_msg=k)
    assert base.tick_time(0) == START + 60 * MIN


def test_tsm_and_fund_triggers(tmp_path):
    hist = tmp_path / "hist"
    _world(hist, n=2)
    feats = {}
    for s in ("BTCUSDT", "S0USDT", "S1USDT"):
        b = load_symbol(hist, s, START, START + DAYS * 1440 * MIN)
        ft = START + 8 * 3_600_000 * np.arange(1, DAYS * 3, dtype=np.int64)
        fr = np.full(len(ft), 0.0001)
        if s == "S0USDT":
            fr[ft > START + 12 * DAY] = 0.0008                # crowded longs after the pump starts
        feats[s] = compute_slow_features(b, (ft, fr))
    add_btc_relative(feats)
    props = scan_slow(feats, LOOSE, tradable_top=3)
    tsm = [p for p in props if p["family"] == "TSM"]
    assert any(p["symbol"] == "S0USDT" and p["side"] == 1 for p in tsm)      # up-trend breakout
    assert any(p["symbol"] == "S1USDT" and p["side"] == -1 for p in tsm)     # down-trend breakdown
    fund = [p for p in props if p["family"] == "FUND"]
    assert fund and all(p["symbol"] == "S0USDT" and p["side"] == -1 for p in fund)
    assert all(p["t_decision"] > START + 12 * DAY for p in fund)
    by = {}
    for p in tsm:                                            # 24 h cooldown per symbol x family
        by.setdefault(p["symbol"], []).append(p["t_decision"])
    assert all(np.all(np.diff(sorted(v)) >= DAY) for v in by.values())
    assert all(p["cost_r"] <= LOOSE.max_cost_r and p["horizon_min"] in (1440, 2880) for p in props)


def test_run_a0_slow_end_to_end(tmp_path):
    hist = tmp_path / "hist"
    _world(hist)
    syms = ["BTCUSDT"] + [f"S{i}USDT" for i in range(7)]
    cfg = A0Config(hist_root=hist, symbols=syms, start=date(2024, 1, 11), end=date(2024, 1, 1) + timedelta(days=DAYS),
                   tradable_top=8, bootstrap=50, arm_kind="slow", slow=LOOSE, warmup_days=10)
    s = run_a0(cfg, tmp_path / "o")
    assert s["arm"] == "A0-slow" and s["feature_schema"] == "f.slow.v1"
    xsm = [g for k, g in s["groups"].items() if k.startswith("XSM_")]
    assert xsm and all(g["n"] > 0 for g in xsm)
    assert "pass_99" in s["verdict"] and "net 99% lo" in (tmp_path / "o" / "summary.md").read_text(encoding="utf-8")
