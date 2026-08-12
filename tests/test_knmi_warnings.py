from datetime import datetime, timedelta, timezone

import pytest

from app.knmi_warnings import format_active_from, parse_utrecht_warnings


def _make_root(ts_id, phen_id, location_id, color):
    from xml.etree import ElementTree as ET

    root = ET.Element("product")
    timeslice = ET.SubElement(root, "timeslice")
    ET.SubElement(timeslice, "timeslice_id").text = ts_id
    phenomenon = ET.SubElement(timeslice, "phenomenon")
    ET.SubElement(phenomenon, "phenomenon_id").text = phen_id
    location = ET.SubElement(phenomenon, "location")
    ET.SubElement(location, "location_id").text = location_id
    ET.SubElement(location, "color_id").text = color
    text = ET.SubElement(location, "text")
    ET.SubElement(text, "text_header").text = "Test header"
    ET.SubElement(text, "text_data").text = "Test omschrijving."
    return root


def test_is_current_false_for_warning_that_only_starts_tomorrow():
    tomorrow_9am = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    ).isoformat()
    root = _make_root(tomorrow_9am, "TX", "UT", "YELLOW")

    results = parse_utrecht_warnings(root)

    assert len(results) == 1
    assert results[0]["is_current"] is False
    assert results[0]["active_from"] == tomorrow_9am


def test_is_current_true_for_warning_active_in_the_current_hour():
    # timeslices dekken een heel uur -- het blokje dat 20 minuten geleden
    # begon loopt nog, dus de waarschuwing is nu actief ondanks dat
    # ts_time zelf in het verleden ligt.
    current_hour_start = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    root = _make_root(current_hour_start, "TX", "UT", "YELLOW")

    results = parse_utrecht_warnings(root)

    assert results[0]["is_current"] is True


def test_warning_fully_in_the_past_is_dropped_entirely():
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    root = _make_root(two_hours_ago, "TX", "UT", "YELLOW")

    results = parse_utrecht_warnings(root)

    assert results == []


def test_format_active_from_today_tomorrow_and_later():
    now = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    assert format_active_from("2026-08-12T14:00:00+02:00", now) == "vandaag 14:00 uur"
    assert format_active_from("2026-08-13T09:00:00+02:00", now) == "morgen 09:00 uur"
    assert format_active_from("2026-08-15T09:00:00+02:00", now) == "za 09:00 uur"


@pytest.fixture()
def _future_yellow_warning_row(temp_db):
    conn = temp_db.get_conn()
    tomorrow_9am_local = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=7, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
    )
    conn.execute(
        """INSERT INTO knmi_warnings
           (phenomenon_id, phenomenon_label, color, color_label,
            active_from, worst_at, header, description, last_updated)
           VALUES ('TX', 'Hitte', 'YELLOW', 'Geel', :active_from, :active_from,
                   'Temperatuur hele land', 'Het Nationaal Hitteplan is actief.', :now)""",
        {"active_from": tomorrow_9am_local.isoformat(), "now": int(datetime.now(timezone.utc).timestamp())},
    )
    conn.commit()
    conn.close()
    return tomorrow_9am_local


def test_api_weather_warnings_marks_future_warning_as_not_current(client, _future_yellow_warning_row):
    data = client.get("/api/weather-warnings").get_json()

    assert data["count"] == 1
    assert data["warnings"][0]["is_current"] is False


def test_lite_api_weather_warnings_marks_future_warning_as_not_current(temp_db, _future_yellow_warning_row):
    from app import lite_server

    lite_server.app.testing = True
    lite_client = lite_server.app.test_client()

    data = lite_client.get("/lite/api/weather-warnings").get_json()

    assert data["count"] == 1
    assert data["warnings"][0]["is_current"] is False
