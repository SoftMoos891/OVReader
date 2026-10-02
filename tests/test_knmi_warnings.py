from datetime import datetime, timedelta, timezone

import pytest

from app.knmi_warnings import format_active_from

# De tests van het lezen van de waarschuwingen staan sinds 2 okt 2026 in de bridge
# (rssbridge/tests/test_knmi_ophalen.py).


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
