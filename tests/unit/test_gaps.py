from jevbot.recorder.gaps import KlineTracker

M = 60_000
T0 = 1_800_000_000_000 - 1_800_000_000_000 % M


def test_continuity_gap_and_duplicates():
    tr = KlineTracker()
    tr.add("X", T0 + 10_000)                     # first observable bar: T0
    assert tr.on_kline("X", T0) == (True, None)
    assert tr.on_kline("X", T0) == (False, None) and tr.duplicates == 1
    ok, gap = tr.on_kline("X", T0 + 4 * M)
    assert ok and gap == (T0 + M, T0 + 3 * M)
    assert tr.on_kline("Y", T0) == (False, None)  # untracked


def test_first_bar_missing_is_a_gap_but_history_before_start_is_not():
    tr = KlineTracker()
    tr.add("X", T0 + 30_000)
    ok, gap = tr.on_kline("X", T0 + 2 * M)
    assert gap == (T0, T0 + M)


def test_watchdog_overdue_and_fill():
    tr = KlineTracker()
    tr.add("X", T0 + 1000)
    grace, after = 5000, 20_000
    # bar T0 closes at T0+60s; not overdue before close+grace+after
    assert tr.overdue(T0 + M + 10_000, grace, after) == []
    over = tr.overdue(T0 + 2 * M + 30_000, grace, after)
    assert over == [("X", T0, T0 + M)]
    tr.mark_filled("X", T0 + M)
    assert tr.overdue(T0 + 2 * M + 30_000, grace, after) == []
    assert tr.fresh_count(T0 + 2 * M + 30_000, grace) == 1
    tr.defer("X", T0 + 10 * M)
    assert tr.overdue(T0 + 5 * M, grace, after) == []
