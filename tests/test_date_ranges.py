from datetime import date

from app import server


def test_this_month_runs_from_first_of_month_to_today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 10)

    monkeypatch.setattr(server, "date", FixedDate)

    since, until = server._date_bounds_for_range("this_month")
    assert since == "2026-07-01"
    assert until == "2026-07-10"


def test_last_month_full_range(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 10)

    monkeypatch.setattr(server, "date", FixedDate)

    since, until = server._date_bounds_for_range("last_month")
    assert since == "2026-06-01"
    assert until == "2026-06-30"


def test_last_month_handles_january_crossing_year_boundary(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 1, 15)

    monkeypatch.setattr(server, "date", FixedDate)

    since, until = server._date_bounds_for_range("last_month")
    assert since == "2025-12-01"
    assert until == "2025-12-31"
