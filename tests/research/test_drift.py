import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from jevbot.research.data import MIN
from jevbot.research.drift import drift_table, forward_r, run_drift
from jevbot.research.synthetic import make_symbol

START = 1_704_067_200_000


def _p(t, side, family="BRK"):
    return {"symbol": "S0USDT", "t_decision": t, "side": side, "stop_dist": 1.0, "family": family, "cost_r": 0.1}


def test_forward_r_matches_price_path(tmp_path):
    hist = tmp_path / "hist"
    make_symbol(hist, "S0USDT", START, 5 * 1440, seed=2, events=[(1000, 0.05, 60)])     # +5 % over 1 h
    ticks = [START + m * MIN for m in range(995, 1000)]        # entry at the open of these minutes
    props = [_p(t, 1) for t in ticks] + [_p(t, -1) for t in ticks] + \
            [_p(START + 5 * 1440 * MIN - 30 * MIN, 1, "PB")]
    fr = forward_r(props, hist, horizons=(15, 60, 120))
    b = pq.read_table(next((hist / "klines" / "1m").rglob("*.parquet"))).to_pydict()
    assert abs(fr[0, 1] - (b["close"][995 + 59] - b["open"][995])) < 1e-9 and fr[0, 1] > 0
    assert np.allclose(fr[5:10], -fr[0:5])                     # side flips the sign
    assert np.isfinite(fr[10, 0]) and np.isnan(fr[10, 2])      # horizon beyond the data -> NaN
    t = drift_table(props, fr, horizons=(15, 60, 120), n_boot=50)
    assert set(t) == {"all", "BRK_long", "BRK_short", "PB_long"}
    assert t["BRK_long"][1]["mean_gross_r"] > 0 and t["BRK_short"][1]["mean_gross_r"] < 0
    assert t["PB_long"][0]["n"] == 1 and "mean_gross_r" not in t["PB_long"][0]      # too few -> no stats


def test_run_drift_cli_outputs(tmp_path):
    from jevbot.cli import main
    hist = tmp_path / "hist"
    make_symbol(hist, "S0USDT", START, 4 * 1440, seed=3)
    rows = [{"symbol": "S0USDT", "t_decision": START + (100 + 50 * i) * MIN, "side": 1 if i % 2 else -1,
             "stop_dist": 0.5, "family": "BRK", "cost_r": 0.1, "exit_type": "SL", "selected": True}
            for i in range(20)]
    pq.write_table(pa.Table.from_pylist(rows), tmp_path / "proposals.parquet")
    s = run_drift(tmp_path / "proposals.parquet", hist, tmp_path / "o")
    assert s["n"] == 20 and (tmp_path / "o" / "drift.md").exists()
    assert main(["research-drift", "--proposals", str(tmp_path / "proposals.parquet"), "--hist", str(hist),
                 "--out", str(tmp_path / "o2"), "--set", "logging.json=false"]) == 0
