"""Tests voor de vertrekkenlijst per halte (Timetable.next_departures).

Draait om wat er wél en niet als vertrek getoond mag worden: bij de
eindhalte van een rit kun je niet instappen, en een U-Flex-rit rijdt alleen
na reservering. Zie find_stop_times_for_trips() in build_static_index.py
voor hoe pickup_type in de opgeslagen halte-tijden terechtkomt.
"""
from datetime import datetime

import pytest

from app.timetable import Timetable

VANDAAG = datetime(2026, 8, 17, 12, 0)  # een maandag
ALLE_DAGEN = [True] * 7


def _timetable(stop_times, trip_meta, monkeypatch):
    """Timetable zonder reload() -- die leest data/-bestanden van schijf."""
    tt = object.__new__(Timetable)
    tt.stops = {"halte-1": {"name": "Utrecht, Testhalte", "lat": 52.0, "lon": 5.0}}
    tt.stop_group_members = {"Utrecht, Testhalte": ["halte-1"]}
    tt.stop_times = stop_times
    tt.trip_meta = trip_meta
    tt.calendar = {
        "S1": {"days": ALLE_DAGEN, "start_date": "20260101", "end_date": "20261231",
               "added": [], "removed": []},
    }
    tt.loaded_at = 0
    # Live vertraging komt uit de database; die is hier niet aan de orde.
    monkeypatch.setattr(tt, "_live_delay_by_trip", lambda *a, **kw: {})
    return tt


def _meta(headsign="Ergens"):
    return {"route_id": "R1", "service_id": "S1", "headsign": headsign}


@pytest.fixture()
def now_ts():
    return int(VANDAAG.timestamp())


def test_normal_departure_is_listed(monkeypatch, now_ts):
    tt = _timetable(
        {"halte-1": [("t1", 3, "12:10:00")]},
        {"t1": _meta("Vleuten")},
        monkeypatch,
    )

    deps = tt.next_departures("halte-1", now_ts)
    assert [d["headsign"] for d in deps] == ["Vleuten"]
    assert deps[0]["on_demand"] is False


def test_on_demand_departure_is_flagged(monkeypatch, now_ts):
    """Een vierde element (waarde 2) markeert een U-Flex-rit."""
    tt = _timetable(
        {"halte-1": [("t1", 3, "12:10:00", 2)]},
        {"t1": _meta()},
        monkeypatch,
    )

    deps = tt.next_departures("halte-1", now_ts)
    assert len(deps) == 1
    assert deps[0]["on_demand"] is True


def test_on_demand_and_normal_departures_side_by_side(monkeypatch, now_ts):
    tt = _timetable(
        {"halte-1": [("t1", 3, "12:10:00"), ("t2", 4, "12:20:00", 2)]},
        {"t1": _meta("Gewoon"), "t2": _meta("Op afroep")},
        monkeypatch,
    )

    by_headsign = {d["headsign"]: d["on_demand"] for d in tt.next_departures("halte-1", now_ts)}
    assert by_headsign == {"Gewoon": False, "Op afroep": True}


def test_stop_that_is_only_a_terminus_has_no_departures(monkeypatch, now_ts):
    """Haltes waar alleen uitgestapt wordt houden een lege lijst in
    stop_times (de halte zelf blijft wél bestaan, zodat 'ie vindbaar blijft
    in het zoeken-op-naam) -- dat mag geen fout geven."""
    tt = _timetable({"halte-1": []}, {}, monkeypatch)

    assert tt.next_departures("halte-1", now_ts) == []


def test_departure_outside_the_time_window_is_not_listed(monkeypatch, now_ts):
    tt = _timetable(
        {"halte-1": [("t1", 3, "23:30:00")]},
        {"t1": _meta()},
        monkeypatch,
    )

    assert tt.next_departures("halte-1", now_ts, window_minutes=30) == []


def test_departure_on_an_inactive_service_day_is_not_listed(monkeypatch, now_ts):
    tt = _timetable(
        {"halte-1": [("t1", 3, "12:10:00")]},
        {"t1": {"route_id": "R1", "service_id": "S-BESTAAT-NIET", "headsign": "X"}},
        monkeypatch,
    )

    assert tt.next_departures("halte-1", now_ts) == []
