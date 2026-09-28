from datetime import datetime, timezone

import numpy as np

from jevbot.research.a1 import A1Config, a1_report, features, fit_logistic, predict, to_markdown, walk_forward
from jevbot.research.data import DAY

T0 = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _rows(n=3000, seed=3, signal=True):
    """Synthetic proposals over ~15 months; with ``signal`` a side-aligned 24 h move predicts the win."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        t = T0 + int(i * 450 * DAY / n)
        side = 1 if rng.random() < 0.5 else -1
        ret = rng.normal(0, 0.03)
        edge = 0.8 * side * ret / 0.006 if signal else 0.0
        win = rng.random() < 1 / (1 + np.exp(-edge))
        net = abs(rng.normal(0.5, 0.2)) * (1 if win else -1)
        rows.append({"t_decision": t, "t_exit": t + DAY, "symbol": f"S{i % 40}", "family": "TSM", "side": side,
                     "atr_pct": 0.006, "ret_24h": ret, "ret_7d": rng.normal(0, 0.08), "rel_ret_7d": 0.0,
                     "ema_trend": side, "run_24h_atr": ret / 0.006, "funding_bps_8h": 1.0, "qv_24h": 5e7,
                     "vol_rank": 10, "listing_age_d": 400, "cost_r": 0.03, "stop_dist_bps": 180.0,
                     "net_r": net, "y_success": int(net > 0.6), "exit_type": "TP" if win else "SL"})
    return rows


def test_logistic_recovers_a_signal():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(4000, 3))
    y = (rng.random(4000) < 1 / (1 + np.exp(-(2 * X[:, 0] - X[:, 1])))).astype(float)
    w, mu, sd = fit_logistic(X, y, lam=1.0)
    assert w[1] > 1.5 and w[2] < -0.6 and abs(w[3]) < 0.2
    p = predict(w, mu, sd, X)
    assert ((p > 0.5) == (y > 0.5)).mean() > 0.7


def test_features_are_side_aligned():
    r = _rows(2)
    long, short = dict(r[0], side=1, ret_24h=0.03), dict(r[0], side=-1, ret_24h=-0.03)
    X, names = features([long, short])
    i = names.index("side*ret_24h_atr")
    assert X[0, i] == X[1, i] == 5.0
    assert X[0, names.index("TSM_long")] == 1.0 and X[1, names.index("TSM_short")] == 1.0


def test_walk_forward_is_purged_and_finds_signal():
    rows = _rows()
    cfg = A1Config(bootstrap=200)
    scored = walk_forward(rows, cfg)
    assert scored and all(r["train_max_exit"] < r["fold"] - cfg.embargo_days * DAY + 1 for r in scored)
    assert min(r["t_decision"] for r in scored) >= int(datetime(2024, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)
    rep = a1_report(scored, cfg)
    assert rep["purge_ok"] and rep["auc_net_win"] > 0.6
    assert rep["A1_selected"]["mean_net_r"] > rep["A0_all"]["mean_net_r"]
    assert "Verdict" in to_markdown(rep)


def test_no_signal_no_selection_gain():
    rep = a1_report(walk_forward(_rows(signal=False), A1Config(bootstrap=200)), A1Config(bootstrap=200))
    assert abs(rep["auc_net_win"] - 0.5) < 0.05 and not rep["verdict"]["pass"]
