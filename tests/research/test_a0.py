from datetime import date, timedelta

import orjson
import pyarrow.parquet as pq

from jevbot.research.a0 import A0Config, block_bootstrap_ci, portfolio_sim, run_a0
from jevbot.research.scanner import ScannerConfig
from jevbot.research.synthetic import make_symbol

D0 = date(2024, 1, 1)
START = 1_704_067_200_000
DAYS = 20


def test_block_bootstrap_ci_brackets_mean():
    import numpy as np
    v = np.random.default_rng(1).normal(0.2, 1.0, 400)
    d = np.repeat(np.arange(40), 10)
    lo, hi = block_bootstrap_ci(v, d, 1000, 3)
    assert lo < v.mean() < hi and hi - lo < 0.5


def test_portfolio_sim_respects_capacity():
    rows = [{"t_decision": t, "t_exit": t + 100, "symbol": s, "net_r": 1.0, "gross_r": 1.1, "fee_r": 0.1, "rank": 1}
            for t, s in [(0, "A"), (0, "B"), (0, "C"), (0, "D"), (10, "A"), (150, "A")]]
    p = portfolio_sim(rows, 3)
    assert p["trades"] == 4                                   # D rejected (full), A@10 rejected (open), A@150 ok


def test_run_a0_end_to_end(tmp_path):
    hist = tmp_path / "hist"
    btc = make_symbol(hist, "BTCUSDT", START, DAYS * 1440, seed=1, base=60000)
    for i in range(3):
        make_symbol(hist, f"S{i}USDT", START, DAYS * 1440, seed=5 + i, btc=btc, beta=1.0,
                    events=[(15 * 1440 + 97 * i, 0.06 * (1 if i % 2 == 0 else -1), 45)])
    cfg = A0Config(hist_root=hist, symbols=["BTCUSDT", "S0USDT", "S1USDT", "S2USDT"], start=D0,
                   end=D0 + timedelta(days=DAYS), tradable_top=4, bootstrap=200,
                   scanner=ScannerConfig(min_qv_24h=0, min_listing_age_d=0))
    s = run_a0(cfg, tmp_path / "out")
    assert s["proposals"] > 0 and s["labelled"] > 0
    assert set(s["verdict"]) >= {"gross_edge", "net_edge"}
    t = pq.read_table(tmp_path / "out" / "proposals.parquet")
    assert t.num_rows == s["proposals"] and {"net_r", "gross_r", "exit_type", "cost_r"} <= set(t.column_names)
    assert orjson.loads((tmp_path / "out" / "summary.json").read_bytes())["arm"] == "A0"
    assert "Verdict" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert s["universe"]["mode"] == "pit_daily" and s["universe"]["pool"] == 4
    g = s["groups"]["all"]
    assert g["mean_net_r"] < g["mean_gross_r"]                # costs always reduce R


def test_cli_research_a0(tmp_path):
    from jevbot.cli import main
    hist = tmp_path / "hist"
    btc = make_symbol(hist, "BTCUSDT", START, DAYS * 1440, seed=1, base=60000)
    make_symbol(hist, "S0USDT", START, DAYS * 1440, seed=5, btc=btc, beta=1.0, events=[(15 * 1440, 0.06, 45)])
    (tmp_path / "pool.json").write_bytes(orjson.dumps({"pool": ["S0USDT"]}))
    code = main(["research-a0", "--hist", str(hist), "--symbols", f"@{tmp_path / 'pool.json'}", "--start",
                 "2024-01-01", "--end", "2024-01-21", "--out", str(tmp_path / "o"), "--set", "logging.json=false"])
    assert code == 0 and (tmp_path / "o" / "summary.json").exists()
