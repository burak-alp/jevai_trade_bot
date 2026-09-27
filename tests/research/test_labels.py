import numpy as np

from jevbot.research.data import MIN, SymbolBars
from jevbot.research.labels import label_one
from jevbot.research.scanner import CostModel

ZERO = CostModel(fee_taker_bps=0.0, spread_bps_by_rank=((10**6, 0.0),), slip_bps_by_rank=((10**6, 0.0),))
NOF = (np.array([], dtype=np.int64), np.array([], dtype=float))


def bars(path_hl, opens=None, mark=None):
    """path_hl: list of (high, low, close) per minute; open = previous close unless given."""
    h = np.array([x[0] for x in path_hl], float)
    lo = np.array([x[1] for x in path_hl], float)
    c = np.array([x[2] for x in path_hl], float)
    o = np.array(opens, float) if opens is not None else np.concatenate([[100.0], c[:-1]])
    mh, ml = (np.array(mark[0], float), np.array(mark[1], float)) if mark else (np.full_like(h, np.nan),) * 2
    z = np.zeros_like(h)
    return SymbolBars("X", 0, o, h, lo, c, z, z, z, mh, ml, 0)


def prop(side=1, horizon=5, spread=0.0, slip=0.0):
    return {"side": side, "t_decision": 0, "horizon_min": horizon, "stop_price": 99.0 if side > 0 else 101.0,
            "tp_price": 101.5 if side > 0 else 98.5, "stop_dist": 1.0, "spread_bps": spread, "slip_bps": slip}


def test_tp_first():
    b = bars([(100.5, 99.5, 100.2), (101.6, 100.1, 101.4), (101.0, 98.0, 98.5), (100, 99, 99), (100, 99, 99)])
    r = label_one(prop(), b, NOF, ZERO)
    assert r["exit_type"] == "TP" and r["y_success"] == 1 and abs(r["gross_r"] - 1.5) < 1e-9 and r["hold_min"] == 2


def test_same_minute_is_stop_first_and_gap_fills_at_open():
    b = bars([(100.5, 99.5, 100.0), (101.6, 98.9, 100.0)] + [(100, 100, 100)] * 3)
    r = label_one(prop(), b, NOF, ZERO)
    assert r["exit_type"] == "SL" and abs(r["gross_r"] + 1.0) < 1e-9
    g = bars([(100.5, 99.5, 99.8), (98.2, 97.5, 98.0)] + [(98, 98, 98)] * 3, opens=[100.0, 98.1, 98, 98, 98])
    r = label_one(prop(), g, NOF, ZERO)
    assert r["exit_type"] == "SL" and abs(r["gross_r"] - (-1.9)) < 1e-9          # gap: open 98.1, not 99


def test_stop_uses_mark_price():
    path = [(100.5, 98.9, 100.0)] + [(100.3, 99.8, 100.0)] * 4        # last price wicks below the stop
    b = bars(path, mark=([100.4] * 5, [99.5] * 5))                      # mark never reaches it
    assert label_one(prop(), b, NOF, ZERO)["exit_type"] == "TIME"
    b2 = bars(path, mark=([100.4] * 5, [98.95] + [99.5] * 4))
    assert label_one(prop(), b2, NOF, ZERO)["exit_type"] == "SL"


def test_short_timeout_costs_and_funding():
    b = bars([(100.2, 99.8, 100.0)] * 4 + [(100.0, 99.4, 99.5)])
    cm = CostModel(fee_taker_bps=5.0)
    p = prop(side=-1, spread=2.0, slip=1.0)
    ft = np.array([2 * MIN], dtype=np.int64)
    r = label_one(p, b, (ft, np.array([0.001])), cm)
    assert r["exit_type"] == "TIME" and abs(r["gross_r"] - 0.5) < 1e-9          # short: 100 -> 99.5
    assert r["funding_r"] < 0 and r["net_r"] < r["gross_r"]                     # short receives positive funding
    assert abs(r["funding_r"] - (-0.1)) < 1e-9                                  # -1 * 0.001 * 100 / 1


def test_data_gap_excluded():
    b = bars([(100.5, 99.5, 100.2), (np.nan, np.nan, np.nan)] + [(100, 100, 100)] * 3)
    assert label_one(prop(), b, NOF, ZERO)["exit_type"] == "data_gap"
