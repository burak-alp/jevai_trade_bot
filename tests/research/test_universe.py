from datetime import date

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from jevbot.recorder.integrity import append_manifest, sha256_file
from jevbot.research.data import DAY, MIN
from jevbot.research.universe import candidate_pool, daily_universe_mask

D0 = 1_704_067_200_000                                         # 2024-01-01


def test_mask_uses_rank_at_first_tick_of_the_day():

    t = np.array([D0 + 5 * MIN, D0 + 10 * MIN, D0 + DAY + 5 * MIN, D0 + DAY + 10 * MIN])
    rank = np.array([[1, 2], [2, 1], [2, 1], [1, 2]])          # intraday rank changes are ignored
    m = daily_universe_mask(rank, t, 1)
    assert m.tolist() == [[True, False], [True, False], [False, True], [False, True]]


def _daily(root, sym, qv):
    ot = D0 - DAY + DAY * np.arange(len(qv), dtype=np.int64)       # from 2023-12-31
    p = root / "klines" / "1d" / f"symbol={sym}" / f"{sym}-klines-1d-all.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"open_time": ot, "quote_volume": np.array(qv, float)}), p)
    append_manifest(root / "klines" / "1d", {"file": p.relative_to(root / "klines" / "1d").as_posix(),
                                             "sha256": sha256_file(p), "symbol": sym, "quality": "ok"})


def test_candidate_pool_is_prior_day_top_n_including_delisted(tmp_path):
    _daily(tmp_path, "AAA", [10, 10, 1, 1])
    _daily(tmp_path, "BBB", [1, 1, 10, 10])
    _daily(tmp_path, "DEAD", [5, np.nan, np.nan, np.nan])          # delisted after day -1
    r = candidate_pool(tmp_path, date(2024, 1, 1), date(2024, 1, 4), top=1)
    assert r["per_day"] == {"2024-01-01": ["AAA"], "2024-01-02": ["AAA"], "2024-01-03": ["BBB"]}
    r2 = candidate_pool(tmp_path, date(2024, 1, 1), date(2024, 1, 4), top=2)
    assert r2["per_day"]["2024-01-01"] == ["AAA", "DEAD"] and set(r2["pool"]) == {"AAA", "BBB", "DEAD"}
    assert r2["days_short"] == []
