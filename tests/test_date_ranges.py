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


# ── 'month:YYYY-MM' -- de maandkiezer op /uitval ──────────────────────────

def _freeze_today(monkeypatch, year, month, day):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(year, month, day)

    monkeypatch.setattr(server, "date", FixedDate)


def test_month_key_returns_full_calendar_month_in_the_past(monkeypatch):
    _freeze_today(monkeypatch, 2026, 8, 15)

    since, until = server._date_bounds_for_range("month:2026-07")
    assert since == "2026-07-01"
    assert until == "2026-07-31"


def test_month_key_for_february_in_a_leap_year(monkeypatch):
    _freeze_today(monkeypatch, 2026, 8, 15)

    since, until = server._date_bounds_for_range("month:2024-02")
    assert since == "2024-02-01"
    assert until == "2024-02-29"


def test_month_key_for_december_crosses_year_boundary_correctly(monkeypatch):
    """December moet naar 1 januari van het jaar erna doorrekenen om de
    laatste dag te vinden -- niet naar maand 13."""
    _freeze_today(monkeypatch, 2027, 3, 1)

    since, until = server._date_bounds_for_range("month:2026-12")
    assert since == "2026-12-01"
    assert until == "2026-12-31"


def test_month_key_for_current_month_stops_at_today(monkeypatch):
    """De lopende maand mag geen einddatum in de toekomst opleveren."""
    _freeze_today(monkeypatch, 2026, 8, 15)

    since, until = server._date_bounds_for_range("month:2026-08")
    assert since == "2026-08-01"
    assert until == "2026-08-15"


def test_malformed_month_key_falls_back_to_current_month(monkeypatch):
    """Een kapotte ?range=-parameter mag geen 500 geven."""
    _freeze_today(monkeypatch, 2026, 8, 15)

    for bad in ("month:onzin", "month:2026-13", "month:", "month:2026"):
        since, until = server._date_bounds_for_range(bad)
        assert (since, until) == ("2026-08-01", "2026-08-15"), bad
