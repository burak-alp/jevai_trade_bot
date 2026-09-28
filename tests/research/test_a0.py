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


def _world(tmp_path):
    hist = tmp_path / "hist"
    btc = make_symbol(hist, "BTCUSDT", START, DAYS * 1440, seed=1, base=60000)
    for i in range(3):
        make_symbol(hist, f"S{i}USDT", START, DAYS * 1440, seed=5 + i, btc=btc, beta=1.0,
                    events=[(15 * 1440 + 97 * i, 0.06 * (1 if i % 2 == 0 else -1), 45),
                            (6 * 1440 + 50 * i, 0.05 * (-1 if i % 2 == 0 else 1), 40)])
    return hist


def test_chunked_run_matches_single_chunk(tmp_path):
    """Warm-up makes chunk features identical; only the cooldown resets at chunk boundaries."""
    hist = _world(tmp_path)
    base = dict(hist_root=hist, symbols=["BTCUSDT", "S0USDT", "S1USDT", "S2USDT"], start=D0,
                end=D0 + timedelta(days=DAYS), tradable_top=4, bootstrap=50,
                scanner=ScannerConfig(min_qv_24h=0, min_listing_age_d=0))
    run_a0(A0Config(**base, chunk_days=100), tmp_path / "one")
    run_a0(A0Config(**base, chunk_days=4), tmp_path / "chunks")
    key = lambda r: (r["t_decision"], r["symbol"], r["family"], r["side"])  # noqa: E731
    one = {key(r): r for r in pq.read_table(tmp_path / "one" / "proposals.parquet").to_pylist()}
    ch = {key(r): r for r in pq.read_table(tmp_path / "chunks" / "proposals.parquet").to_pylist()}
    assert one and set(one) <= set(ch)
    bounds = [START + k * 4 * 86_400_000 for k in range(1, DAYS // 4)]
    cooldown = 6 * 5 * 60_000
    assert all(any(0 < k[0] - b <= cooldown for b in bounds) for k in set(ch) - set(one))
    for k, r in one.items():
        assert abs(r["stop_dist"] - ch[k]["stop_dist"]) < 1e-9 and r["exit_type"] == ch[k]["exit_type"]
        if r["exit_type"] in ("TP", "SL", "TIME"):
            assert abs(r["net_r"] - ch[k]["net_r"]) < 1e-9


def test_per_day_pool_and_pit_listing_age(tmp_path):
    import pyarrow as pa
    hist = _world(tmp_path)
    (hist / "_pit").mkdir()
    pq.write_table(pa.table({"symbol": ["S0USDT", "S1USDT", "S2USDT", "BTCUSDT"],
                             "first_date": ["2023-01-01", "2023-01-01", "2024-01-10", "2020-01-01"]}),
                   hist / "_pit" / "symbol_listing-klines-1m.parquet")
    per_day = {str(D0 + timedelta(days=d)): (["S0USDT", "S2USDT"] if d >= 10 else ["S0USDT"]) for d in range(DAYS)}
    cfg = A0Config(hist_root=hist, symbols=[], start=D0, end=D0 + timedelta(days=DAYS), tradable_top=4,
                   bootstrap=50, chunk_days=10, per_day=per_day, scanner=ScannerConfig(min_qv_24h=0))
    s = run_a0(cfg, tmp_path / "o")
    rows = pq.read_table(tmp_path / "o" / "proposals.parquet").to_pylist()
    assert {r["symbol"] for r in rows} <= {"BTCUSDT", "S0USDT", "S2USDT"}          # S1 never loaded
    assert s["universe"]["pool"] == 3
    s0 = [r for r in rows if r["symbol"] == "S0USDT"]
    assert s0 and all(r["listing_age_d"] > 300 for r in s0)                  # PIT date, not data start
    assert all(r["listing_age_d"] >= 14 for r in rows if r["symbol"] == "S2USDT")   # listed 01-10: gated


def test_block_days_groups_calendar_weeks_and_widens_ci_under_overlap():
    import numpy as np
    from jevbot.research.a0 import _block_days, group_stats
    cfg = A0Config(hist_root=None, symbols=[], start=D0, end=D0, bootstrap=2000)
    slow = A0Config(hist_root=None, symbols=[], start=D0, end=D0, arm_kind="slow")
    assert _block_days(cfg) == 1 and _block_days(slow) == 7
    # overlapping multi-day outcomes: a slow regime shared by neighbouring days
    rng = np.random.default_rng(0)
    regime = np.repeat(rng.normal(0, 1.0, 20), 7)            # 140 days, level persists for a week
    rows = [{"t_decision": d * 86_400_000 + k, "net_r": regime[d] + rng.normal(0, 0.3), "gross_r": 0.0,
             "exit_type": "TIME", "y_success": 0, "cost_r": 0.0, "fee_r": 0.0, "funding_r": 0.0,
             "mae_r": 0.0, "mfe_r": 0.0} for d in range(140) for k in range(3)]
    w1 = np.diff(group_stats(rows, cfg)["net_ci95"])[0]
    cfg7 = A0Config(hist_root=None, symbols=[], start=D0, end=D0, bootstrap=2000, ci_block_days=7)
    w7 = np.diff(group_stats(rows, cfg7)["net_ci95"])[0]
    assert w7 > 1.5 * w1                                     # day blocks understate uncertainty here
