import numpy as np

from jevbot.research.data import load_funding, load_symbol
from jevbot.research.synthetic import make_symbol

START = 1_700_006_400_000 - 1_700_006_400_000 % 86_400_000


def test_load_symbol_aligned_grid_and_gaps(tmp_path):
    make_symbol(tmp_path, "AAAUSDT", START + 60 * 60_000, 600, seed=1)       # starts 1 h after grid start
    b = load_symbol(tmp_path, "AAAUSDT", START, START + 1440 * 60_000)
    assert b.n == 1440 and b.first_minute == 60
    assert np.isnan(b.close[:60]).all() and not np.isnan(b.close[60:660]).any() and np.isnan(b.close[660:]).all()
    assert (b.high[60:660] >= b.close[60:660]).all() and not np.isnan(b.mark_low[60:660]).any()
    t, r = load_funding(tmp_path, "AAAUSDT")
    assert len(t) >= 1 and np.all(np.diff(t) > 0) and np.allclose(r, 0.0001)


def test_missing_symbol_is_all_nan(tmp_path):
    b = load_symbol(tmp_path, "NONEUSDT", START, START + 60 * 60_000)
    assert np.isnan(b.close).all() and b.first_minute == -1
