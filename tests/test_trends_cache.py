from datetime import datetime

from app import server


def _ts(y, m, d, h, mi):
    return datetime(y, m, d, h, mi).timestamp()


def test_next_daily_boundary_same_day_before_refresh_time():
    after = _ts(2026, 7, 10, 1, 0)  # 01:00, voor 03:30
    boundary = server._next_daily_boundary(after)
    assert boundary == _ts(2026, 7, 10, 3, 30)


def test_next_daily_boundary_same_day_after_refresh_time():
    after = _ts(2026, 7, 10, 10, 0)  # 10:00, na 03:30
    boundary = server._next_daily_boundary(after)
    assert boundary == _ts(2026, 7, 11, 3, 30)


def test_next_daily_boundary_exactly_at_refresh_time_rolls_to_next_day():
    after = _ts(2026, 7, 10, 3, 30)
    boundary = server._next_daily_boundary(after)
    assert boundary == _ts(2026, 7, 11, 3, 30)


def test_cached_daily_reuses_value_within_same_day(monkeypatch):
    server._response_cache.clear()
    calls = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    now = _ts(2026, 7, 10, 10, 0)
    monkeypatch.setattr(server.time, "time", lambda: now)
    first = server._cached_daily(("test-daily",), compute)
    second = server._cached_daily(("test-daily",), compute)

    assert first == second == {"n": 1}
    assert len(calls) == 1


def test_cached_daily_recomputes_after_crossing_refresh_boundary(monkeypatch):
    server._response_cache.clear()
    calls = []

    def compute():
        calls.append(1)
        return {"n": len(calls)}

    monkeypatch.setattr(server.time, "time", lambda: _ts(2026, 7, 10, 10, 0))
    server._cached_daily(("test-daily-2",), compute)

    # Na 03:30 de volgende dag gepasseerd -- moet opnieuw berekenen.
    monkeypatch.setattr(server.time, "time", lambda: _ts(2026, 7, 11, 4, 0))
    result = server._cached_daily(("test-daily-2",), compute)

    assert result == {"n": 2}
    assert len(calls) == 2
