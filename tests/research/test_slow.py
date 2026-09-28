from datetime import date, timedelta

import numpy as np

from jevbot.research.a0 import A0Config, run_a0
from jevbot.research.data import DAY, MIN, load_funding, load_open_interest, load_symbol
from jevbot.research.slow import (POS_FAMILIES, PosConfig, SlowConfig, add_btc_relative, compute_slow_features, oi_at,
                                  pos_config, scan_slow)
from jevbot.research.synthetic import make_metrics, make_symbol

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
    xsm = [s["groups"][k] for k in ("XSM_long", "XSM_short")]
    assert xsm and all(g["n"] > 0 for g in xsm)
    assert "pass_99" in s["verdict"] and "net 99% lo" in (tmp_path / "o" / "summary.md").read_text(encoding="utf-8")


def _oi_world(hist):
    """S0 pumps with open interest +25 % (crowded longs); S1 dumps while OI falls 12 % in 2 h (flush)."""
    _world(hist, n=2)
    t = START + 5 * MIN * np.arange(DAYS * 288, dtype=np.int64)
    ev0, ev1 = START + 12 * DAY, START + 12 * DAY + 60 * MIN
    oi0 = 1e6 * (1 + 0.25 * np.clip((t - ev0) / (600 * MIN), 0, 1))
    oi1 = 1e6 * (1 - 0.12 * np.clip((t - ev1) / (120 * MIN), 0, 1))
    make_metrics(hist, "S0USDT", t, oi0)
    make_metrics(hist, "S1USDT", t, oi1)
    return ev0, ev1


LOOSE_POS = SlowConfig(min_qv_24h=0, min_listing_age_d=0, families=POS_FAMILIES, pos=PosConfig())


def test_oi_at_lag_and_staleness():
    ot = np.array([0, 5, 10, 60], dtype=np.int64) * MIN
    ov = np.array([1.0, 2.0, 3.0, 4.0])
    got = oi_at((ot, ov), np.array([4, 10, 14, 15, 45, 46, 65, 200], dtype=np.int64) * MIN)
    # 4 min: nothing published yet; 10/14: the 5 min row; 15..45: the 10 min row; 46: stale (> 30 min); 65: 60 min row
    np.testing.assert_array_equal(got, [np.nan, 2.0, 2.0, 3.0, 3.0, np.nan, 4.0, np.nan])


def test_pos_features_no_lookahead_on_open_interest(tmp_path):
    hist = tmp_path / "hist"
    _oi_world(hist)
    b = load_symbol(hist, "S0USDT", START, START + DAYS * 1440 * MIN)
    ot, ov = load_open_interest(hist, "S0USDT")
    base = compute_slow_features(b, oi=(ot, ov))
    cut = START + 13 * DAY
    poisoned = compute_slow_features(b, oi=(ot, np.where(ot >= cut, ov * 5, ov)))
    j = int((cut - START) // (60 * MIN)) - 1                 # tick at the cut sees rows <= cut - 5 min only
    for k in ("oi_chg_24h", "oi_chg_4h"):
        np.testing.assert_array_equal(base.f[k][:j + 1], poisoned.f[k][:j + 1], err_msg=k)
        assert not np.array_equal(base.f[k], poisoned.f[k])


def test_crowd_and_flush_triggers(tmp_path):
    hist = tmp_path / "hist"
    ev0, ev1 = _oi_world(hist)
    feats = {}
    for s in ("BTCUSDT", "S0USDT", "S1USDT"):
        b = load_symbol(hist, s, START, START + DAYS * 1440 * MIN)
        feats[s] = compute_slow_features(b, load_funding(hist, s), load_open_interest(hist, s))
    add_btc_relative(feats)
    props = scan_slow(feats, LOOSE_POS, tradable_top=3)
    crowd = [p for p in props if p["family"] == "CROWD"]
    flush = [p for p in props if p["family"] == "FLUSH"]
    assert crowd and all(p["symbol"] == "S0USDT" and p["side"] == -1 and p["t_decision"] > ev0 for p in crowd)
    assert flush and all(p["symbol"] == "S1USDT" and p["side"] == 1 and p["t_decision"] > ev1 for p in flush)
    assert min(p["t_decision"] for p in flush) <= ev1 + 6 * 3_600_000
    assert all(p["family_version"] in ("crowd.v1", "flush.v1") and p["horizon_min"] == 1440 for p in props)
    assert all(np.isfinite(p["oi_chg_24h"]) and p["btc_trend"] in (-1.0, 0.0, 1.0) for p in props)


def test_slow_v1_config_dict_unchanged_by_pos():
    assert "pos" not in SlowConfig().to_dict()
    assert pos_config().to_dict()["pos"]["crowd_oi_chg"] == 0.15 and pos_config().families == POS_FAMILIES


def test_run_a0_pos_end_to_end(tmp_path):
    hist = tmp_path / "hist"
    _oi_world(hist)
    cfg = A0Config(hist_root=hist, symbols=["BTCUSDT", "S0USDT", "S1USDT"], start=date(2024, 1, 11),
                   end=date(2024, 1, 1) + timedelta(days=DAYS), tradable_top=3, bootstrap=50, arm_kind="pos",
                   slow=LOOSE_POS, warmup_days=10)
    s = run_a0(cfg, tmp_path / "o")
    assert s["arm"] == "A0-pos" and s["feature_schema"] == "f.pos.v1" and s["verdict"]["ci_block_days"] == 7
    assert s["groups"]["CROWD_short"]["n"] > 0 and s["groups"]["FLUSH_long"]["n"] > 0
    assert set(s["groups"]) >= {"CROWD", "FLUSH"} and "TSM" not in s["groups"]
    assert tuple(s["scanner_config"]["families"]) == POS_FAMILIES and s["scanner_config"]["pos"]["flush_oi_chg"] == -0.08


def test_reg_groups_follow_btc_trend(tmp_path):
    hist = tmp_path / "hist"
    _world(hist)
    syms = ["BTCUSDT"] + [f"S{i}USDT" for i in range(7)]
    cfg = A0Config(hist_root=hist, symbols=syms, start=date(2024, 1, 11), end=date(2024, 1, 1) + timedelta(days=DAYS),
                   tradable_top=8, bootstrap=50, arm_kind="slow", slow=LOOSE, warmup_days=10)
    s = run_a0(cfg, tmp_path / "o")
    g = s["groups"]
    for fam in ("TSM", "XSM"):
        for nm in ("long", "short"):
            assert g[f"{fam}_{nm}@reg"]["n"] <= g[f"{fam}_{nm}"]["n"]
    assert sum(v["n"] for k, v in g.items() if k.endswith("_long@reg")) <= g["btc_trend_up"]["n"]
    assert "reg_pass_99" in s["verdict"] and all("@reg" not in k for k in s["verdict"]["pass_99"])
