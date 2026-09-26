from jevbot.core.time import ClockOffsetEstimator, floor_ms, ms_to_date, ms_to_iso


def test_floor_and_format():
    assert floor_ms(1_700_000_012_345, 60_000) == 1_699_999_980_000
    assert ms_to_iso(0) == "1970-01-01T00:00:00.000Z"
    assert ms_to_date(86_400_000) == "1970-01-02"


def test_clock_offset_estimator():
    est = ClockOffsetEstimator(alpha=1.0, max_rtt_ms=500)
    assert est.add_sample(1000, 1150, 1100)          # server 100 ms ahead of midpoint
    assert est.offset_ms == 100
    assert not est.add_sample(1000, 5000, 2000)      # rtt 1000 > 500 rejected
    assert est.rejected == 1 and est.offset_ms == 100
    est2 = ClockOffsetEstimator(alpha=0.5)
    est2.add_sample(0, 10, 0)
    est2.add_sample(0, 20, 0)
    assert est2.offset_ms == 15
