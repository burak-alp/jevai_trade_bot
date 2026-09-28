from datetime import date, timedelta

import numpy as np
import pyarrow as pa

from jevbot.research.carry import CarryConfig, run_carry, spot_map
from jevbot.research.data import DAY
from jevbot.research.synthetic import _write

HOUR = 3_600_000
START = date(2024, 1, 1)
T0 = 1_704_067_200_000                                             # 2024-01-01


def _sym(root, s, rate, days=40, prem=0.0005, drift=0.0):
    ft = T0 - 10 * DAY + 8 * HOUR * np.arange(1, (days + 10) * 3, dtype=np.int64)
    _write(root, f"fundingRate/symbol={s}/{s}-fundingRate-all.parquet",
           pa.table({"calc_time": ft, "funding_interval_hours": np.full(len(ft), 8),
                     "last_funding_rate": np.full(len(ft), rate)}), s, "all")
    ot = T0 - 10 * DAY + HOUR * np.arange((days + 10) * 24, dtype=np.int64)
    one = np.full(len(ot), prem)
    _write(root, f"premiumIndexKlines/1h/symbol={s}/{s}-premiumIndexKlines-1h-all.parquet",
           pa.table({"open_time": ot, "open": one, "high": one, "low": one, "close": one}), s, "all")
    od = T0 - 10 * DAY + DAY * np.arange(days + 10, dtype=np.int64)
    px = 100 * (1 + drift) ** np.arange(len(od))
    _write(root, f"klines/1d/symbol={s}/{s}-klines-1d-all.parquet",
           pa.table({"open_time": od, "open": px, "high": px, "low": px, "close": px}), s, "all")


def test_spot_map_handles_multiplier_prefixes():
    m = spot_map(["BTCUSDT", "1000PEPEUSDT", "1000SATSUSDT", "NOSPOTUSDT"], {"BTCUSDT", "PEPEUSDT", "1000SATSUSDT"})
    assert m == {"BTCUSDT": "BTCUSDT", "1000PEPEUSDT": "PEPEUSDT", "1000SATSUSDT": "1000SATSUSDT"}


def test_carry_enters_high_funding_only_and_accrues_exactly(tmp_path):
    _sym(tmp_path, "AUSDT", 0.0003)                                 # 32.9 % APR -> carry
    _sym(tmp_path, "BUSDT", 0.00005)                                # 5.5 % APR -> never entered
    _sym(tmp_path, "CUSDT", 0.0005)                                 # no spot pair -> excluded
    per_day = {(START + timedelta(days=i)).isoformat(): ["AUSDT", "BUSDT", "CUSDT"] for i in range(30)}
    cfg = CarryConfig(bootstrap=100)
    s = run_carry(tmp_path, per_day, {"AUSDT", "BUSDT"}, {}, START, START + timedelta(days=29), cfg)
    assert s["trades"] == 1 and s["spot_mapped"] == 2
    notional = 0.1 / (1 + 1 / 3)
    # held day 0 -> day 29: 29 days x 3 payments x 0.0003; constant premium -> zero basis; two 21 bps legs
    assert np.isclose(s["sum_funding"], round(29 * 3 * 0.0003 * notional, 4), atol=1e-4)
    assert s["sum_basis"] == 0.0 and np.isclose(s["sum_costs"], round(2 * 21e-4 * notional, 4), atol=1e-4)
    assert s["full"]["apr"] > 0 and s["avg_positions"] > 0.9


def test_rebalance_cost_when_price_rallies(tmp_path):
    _sym(tmp_path, "AUSDT", 0.0003, drift=0.02)                     # +2 %/day -> +20 % after ~10 days
    per_day = {(START + timedelta(days=i)).isoformat(): ["AUSDT"] for i in range(30)}
    s = run_carry(tmp_path, per_day, {"AUSDT"}, {}, START, START + timedelta(days=29), CarryConfig(bootstrap=100))
    assert s["rebalances"] >= 2


def test_fixed_baseline_holds_whole_period(tmp_path):
    _sym(tmp_path, "BTCUSDT", 0.0001)
    _sym(tmp_path, "ETHUSDT", 0.0001)
    s = run_carry(tmp_path, {}, {"BTCUSDT", "ETHUSDT"}, {}, START, START + timedelta(days=20),
                  CarryConfig(bootstrap=100), fixed=["BTCUSDT", "ETHUSDT"])
    assert s["trades"] == 2 and s["avg_positions"] > 1.8
