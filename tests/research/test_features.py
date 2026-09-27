import numpy as np

from jevbot.research.data import load_symbol
from jevbot.research.features import (
    add_market_context,
    compute_symbol_features,
    efficiency_ratio,
    ema,
    resample,
    wilder_atr,
)
from jevbot.research.synthetic import make_symbol

START = 1_700_006_400_000 - 1_700_006_400_000 % 86_400_000
DAYS = 12
M = DAYS * 1440


def _data(tmp_path):
    btc = make_symbol(tmp_path, "BTCUSDT", START, M, seed=1, base=60000)
    make_symbol(tmp_path, "AAAUSDT", START, M, seed=2, btc=btc, beta=1.2, events=[(9000, 0.05, 60)])
    return {s: load_symbol(tmp_path, s, START, START + M * 60_000) for s in ("BTCUSDT", "AAAUSDT")}


def test_primitives():
    x = np.array([1.0, 2, 3, 4, 5, 6])
    e = ema(x, 3)
    assert np.isnan(e[:2]).all() and e[-1] > e[-2]
    er = efficiency_ratio(np.array([1.0, 2, 3, 4, 5]), 4)
    assert er[-1] == 1.0
    h, l, c = np.array([2.0] * 20), np.array([1.0] * 20), np.array([1.5] * 20)
    assert abs(wilder_atr(h, l, c, 14)[-1] - 1.0) < 1e-12


def test_resample_marks_incomplete_bars(tmp_path):
    b = _data(tmp_path)["AAAUSDT"]
    b.close[7] = np.nan
    r5 = resample(b, 5)
    assert np.isnan(r5["close"][1]) and not np.isnan(r5["close"][0])
    assert r5["high"][0] == np.max(b.high[:5])


def test_no_lookahead_future_poisoning(tmp_path):
    """Corrupt everything after minute X: every feature at ticks decided at or before X is unchanged."""
    bars = _data(tmp_path)
    ref = {s: compute_symbol_features(b) for s, b in bars.items()}
    add_market_context(ref)
    X = 9000 + 37                                     # arbitrary cut, not bar aligned
    for b in bars.values():
        rng = np.random.default_rng(99)
        for a in (b.open, b.high, b.low, b.close, b.quote_volume, b.taker_buy_quote):
            a[X:] = a[X:] * rng.uniform(0.5, 1.5, len(a) - X)
    pois = {s: compute_symbol_features(b) for s, b in bars.items()}
    add_market_context(pois)
    last_tick = (X - 5) // 5                          # tick j uses minutes < 5j+5 <= X
    for s in ref:
        for name, v in ref[s].f.items():
            a, p = v[:last_tick + 1], pois[s].f[name][:last_tick + 1]
            assert np.array_equal(a, p, equal_nan=True), (s, name)
        changed = any(not np.array_equal(v[last_tick + 5:], pois[s].f[k][last_tick + 5:], equal_nan=True)
                      for k, v in ref[s].f.items())
        assert changed                                 # the poisoning did reach later ticks


def test_features_sane(tmp_path):
    bars = _data(tmp_path)
    feats = {s: compute_symbol_features(b) for s, b in bars.items()}
    add_market_context(feats)
    f = feats["AAAUSDT"].f
    late = slice(2100, None)
    assert np.nanmedian(f["beta_btc"][late]) > 0.8                   # injected beta 1.2
    assert np.nanmin(f["atr_pct"][late]) > 0
    assert (f["h32"][late] >= f["l32"][late]).all()
    assert np.nanmax(f["ret_1h_atr"][1790:1810]) > 2                 # injected +5 % trend at minute 9000
