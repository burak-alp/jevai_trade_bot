from jevbot.core.time import MINUTE_MS
from jevbot.recorder.report import _kline_completeness


def test_kline_completeness_uses_universe_at_minute_close():
    m = MINUTE_MS
    uni = [
        {"t_asof": 10_000, "symbol": "A", "in_universe": True},
        {"t_asof": m + 30_000, "symbol": "B", "in_universe": True},
    ]
    kl = [
        {"open_time": 0, "symbol": "A", "source": "ws"},
        {"open_time": m, "symbol": "B", "source": "ws"},
        {"open_time": 2 * m, "symbol": "B", "source": "rest"},
    ]
    r = _kline_completeness(kl, uni)
    assert r["cells_missing"] == 0
    assert r["symbols"] == r["symbols_complete"] == 2
    assert r["sources"] == {"ws": 2, "rest": 1}

    r = _kline_completeness(kl[:-1], uni)
    assert r["cells_missing"] == 0  # last minute is outside the observed window

    r = _kline_completeness(kl[:1] + kl[2:], uni)
    assert r["cells_missing"] == 1
    assert r["symbols_complete"] == 1
