from datetime import date, timedelta

import pytest


@pytest.fixture()
def lite_client(temp_db):
    from app import lite_server

    lite_server.app.testing = True
    return lite_server.app.test_client()


def test_lite_page_returns_200(lite_client):
    res = lite_client.get("/lite")
    assert res.status_code == 200


def test_lite_alerts_returns_active_alerts_with_route_meta(lite_client, temp_db, monkeypatch):
    from app import lite_server

    monkeypatch.setitem(lite_server._index.routes, "ROUTE_K", {"short_name": "1", "operator": "Keolis"})

    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO alerts (alert_id, first_seen, last_seen, route_ids, header, description, effect, active)
           VALUES ('a1', 100, 100, 'ROUTE_K', 'Grote verstoring op lijn 1', 'Rijdt niet', 'NO_SERVICE', 1)"""
    )
    conn.commit()
    conn.close()

    data = lite_client.get("/lite/api/alerts").get_json()

    assert data["count"] == 1
    alert = data["alerts"][0]
    assert alert["header"] == "Grote verstoring op lijn 1"
    assert alert["routes"][0]["short_name"] == "1"
    assert alert["routes"][0]["operator"] == "Keolis"


def test_lite_alerts_excludes_inactive(lite_client, temp_db):
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO alerts (alert_id, first_seen, last_seen, route_ids, header, description, effect, active)
           VALUES ('a2', 100, 100, '', 'Oude melding', '', 'OTHER_EFFECT', 0)"""
    )
    conn.commit()
    conn.close()

    data = lite_client.get("/lite/api/alerts").get_json()
    assert data["count"] == 0


def test_lite_uitval_percentage_and_per_operator(lite_client, temp_db, monkeypatch):
    from app import lite_server

    monkeypatch.setitem(lite_server._index.routes, "ROUTE_K", {"short_name": "1", "operator": "Keolis"})
    monkeypatch.setitem(lite_server._index.routes, "ROUTE_T", {"short_name": "2", "operator": "Transdev"})

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 'k1', 'ROUTE_K')",
        (today,),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('k2', ?, 'ROUTE_K', '08:00:00', 0, 0)""",
        (today,),
    )
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 't1', 'ROUTE_T')",
        (today,),
    )
    conn.commit()
    conn.close()

    data = lite_client.get("/lite/api/uitval").get_json()

    assert data["date"] == today
    assert data["total_canceled"] == 1
    assert data["total_ran"] == 2
    assert data["cancellation_pct"] == round(100.0 * 1 / 3, 1)

    per_op = {a["operator"]: a for a in data["per_operator"]}
    assert per_op["Keolis"]["canceled"] == 1
    assert per_op["Keolis"]["ran"] == 1
    assert per_op["Transdev"]["canceled"] == 0
    assert per_op["Transdev"]["ran"] == 1


def test_lite_uitval_excludes_tram(lite_client, temp_db, monkeypatch):
    from app import lite_server
    from app.concession_mapping import TRANSDEV_TRAM

    monkeypatch.setitem(lite_server._index.routes, "TRAM", {"short_name": "20", "operator": TRANSDEV_TRAM})

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 'tr1', 'TRAM')",
        (today,),
    )
    conn.commit()
    conn.close()

    data = lite_client.get("/lite/api/uitval").get_json()
    assert data["total_ran"] == 0
    assert data["per_operator"] == []


def test_lite_uitval_daily_covers_last_14_days_and_splits_by_operator(lite_client, temp_db, monkeypatch):
    from app import lite_server

    monkeypatch.setitem(lite_server._index.routes, "ROUTE_K", {"short_name": "1", "operator": "Keolis"})

    today = date.today()
    yesterday = today - timedelta(days=1)
    too_old = today - timedelta(days=lite_server.CHART_DAYS)  # net buiten het venster

    conn = temp_db.get_conn()
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 'k1', 'ROUTE_K')",
        (today.isoformat(),),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('k2', ?, 'ROUTE_K', '08:00:00', 0, 0)""",
        (yesterday.isoformat(),),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('k3', ?, 'ROUTE_K', '08:00:00', 0, 0)""",
        (too_old.isoformat(),),
    )
    conn.commit()
    conn.close()

    data = lite_client.get("/lite/api/uitval/daily").get_json()

    assert data["since_date"] == (today - timedelta(days=lite_server.CHART_DAYS - 1)).isoformat()
    assert data["until_date"] == today.isoformat()

    by_date = {d["date"]: d for d in data["daily"]}
    assert by_date[today.isoformat()]["ran"] == 1
    assert by_date[yesterday.isoformat()]["canceled"] == 1
    assert too_old.isoformat() not in by_date  # buiten het venster van CHART_DAYS

    keolis_daily = {d["date"]: d for d in data["daily_by_operator"]["Keolis"]}
    assert keolis_daily[yesterday.isoformat()]["cancellation_pct"] == 100.0
