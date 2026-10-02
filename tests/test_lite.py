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


def _wegsituatie(temp_db, sid, active=1, in_feed=True):
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO road_situations (situation_id, first_seen, last_seen, record_type, type_label,
               comment, cause, severity, start_time, end_time, active, lat, lon, road_number, road_location)
           VALUES (?, 1000, 2000, 'Accident', 'Ongeval', NULL, NULL, 'unknown',
                   '2026-09-29T16:15:00Z', NULL, ?, 52.2, 4.98, 'N201', 'Vinkeveen - A2')""",
        (sid, active),
    )
    if in_feed:
        conn.execute(
            """INSERT INTO rss_feed_items (guid, kind, title, description, pub_date, created_at)
               VALUES (?, 'road_situation', 'Wegsituatie (Ongeval): N201 Vinkeveen - A2',
                       'Ongeval op N201 Vinkeveen - A2. Klik hier voor meer data.', 1000, 1000)""",
            (f"road-situation-{sid}",),
        )
    conn.commit()
    conn.close()


def test_melding_wegsituatie_uit_de_feed(lite_client, temp_db):
    _wegsituatie(temp_db, "NDW01_a")
    data = lite_client.get("/lite/api/melding?guid=road-situation-NDW01_a").get_json()
    assert data["kind"] == "road_situation"
    assert data["title"] == "Wegsituatie (Ongeval): N201 Vinkeveen - A2"
    assert data["lat"] == 52.2 and data["road_number"] == "N201"
    assert data["nog_actief"] is True
    assert data["resolved_at"] is None


def test_melding_voorbije_wegsituatie_niet_meer_in_de_feed(lite_client, temp_db):
    """Een wegsituatie wordt bij 'voorbij' uit rss_feed_items gewist; de link in
    een RSS-lezer (naar /verkeer) moet dan nog steeds iets tonen."""
    _wegsituatie(temp_db, "NDW01_b", active=0, in_feed=False)
    res = lite_client.get("/lite/api/melding?guid=road-situation-NDW01_b")
    assert res.status_code == 200
    data = res.get_json()
    assert data["title"] == "Wegsituatie (Ongeval): N201 Vinkeveen - A2"
    assert data["description"] == "Ongeval op N201 Vinkeveen - A2."
    assert data["pub_date"] == 1000
    assert data["resolved_at"] == 2000
    assert data["nog_actief"] is False
    assert data["lat"] == 52.2


def test_melding_onbekend_geeft_404(lite_client, temp_db):
    assert lite_client.get("/lite/api/melding?guid=road-situation-bestaat-niet").status_code == 404
    assert lite_client.get("/lite/api/melding?guid=bus-alert-bestaat-niet").status_code == 404


def test_rss_wegsituatie_linkt_naar_verkeerspagina_rest_naar_lite(lite_client, temp_db):
    """Sinds 29 sep 2026: wegsituaties openen op reader.dvznet.nl/verkeer in een
    pop-up; U-OV-, NS- en uitvalmeldingen blijven naar OV Lite gaan (KNMI sinds 2 okt ook naar
    /verkeer, zie hieronder)."""
    _wegsituatie(temp_db, "NDW01_c")
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO rss_feed_items (guid, kind, title, description, pub_date, created_at)
           VALUES ('bus-alert-KV15:KEOLIS:1', 'bus_alert', 'Melding U-OV: storing', 'x', 900, 900)"""
    )
    conn.commit()
    conn.close()
    xml = lite_client.get("/lite/rss.xml").get_data(as_text=True)
    assert "<link>https://reader.dvznet.nl/verkeer#melding=road-situation-NDW01_c</link>" in xml
    assert "<link>https://ovreader.dvznet.nl/lite#bus-alert-KV15%3AKEOLIS%3A1</link>" in xml


def test_rss_knmi_waarschuwing_linkt_naar_verkeerspagina(lite_client, temp_db):
    """Sinds 2 okt 2026 openen ook KNMI-weerwaarschuwingen op reader.dvznet.nl/verkeer."""
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO rss_feed_items (guid, kind, title, description, pub_date, created_at, category)
           VALUES ('knmi-warning-VV-YELLOW', 'knmi_warning', 'Weerwaarschuwing: code geel (Mist)', 'x', 900, 900, 'geel')"""
    )
    conn.execute(
        """INSERT INTO rss_feed_items (guid, kind, title, description, pub_date, created_at)
           VALUES ('rail-alert-1', 'rail_alert', 'NS-storing: x', 'x', 800, 800)"""
    )
    conn.commit()
    conn.close()
    xml = lite_client.get("/lite/rss.xml").get_data(as_text=True)
    assert "<link>https://reader.dvznet.nl/verkeer#melding=knmi-warning-VV-YELLOW</link>" in xml
    assert "<category>geel</category>" in xml
    assert "<link>https://ovreader.dvznet.nl/lite#rail-alert-1</link>" in xml


def test_feed_items_json_zelfde_als_rss_met_soort(lite_client, temp_db):
    """Sinds 2 okt 2026: /lite/api/feed-items geeft dezelfde meldingen als de RSS-feed,
    met kind erbij, voor de RSS-bridge van de DVZ Reader."""
    _wegsituatie(temp_db, "NDW01_j")
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO rss_feed_items (guid, kind, title, description, pub_date, created_at, category)
           VALUES ('knmi-warning-VV-YELLOW', 'knmi_warning', 'Weerwaarschuwing: code geel (Mist)', 'Mist.', 950, 950, 'geel')"""
    )
    conn.execute(
        """INSERT INTO rss_feed_items (guid, kind, title, description, pub_date, created_at)
           VALUES ('bus-alert-KV15:QBUZZ:9', 'bus_alert', 'Melding U-OV: storing', 'x', 900, 900)"""
    )
    conn.commit()
    conn.close()
    data = lite_client.get("/lite/api/feed-items").get_json()
    assert data["feed_title"] == "OV Utrecht - Storingen en uitval-signalering"
    per_guid = {i["guid"]: i for i in data["items"]}
    assert per_guid["knmi-warning-VV-YELLOW"]["kind"] == "knmi_warning"
    assert per_guid["knmi-warning-VV-YELLOW"]["category"] == "geel"
    assert per_guid["bus-alert-KV15:QBUZZ:9"]["link"] == "https://ovreader.dvznet.nl/lite#bus-alert-KV15%3AQBUZZ%3A9"
    assert per_guid["road-situation-NDW01_j"]["link"] == "https://reader.dvznet.nl/verkeer#melding=road-situation-NDW01_j"
    xml = lite_client.get("/lite/rss.xml").get_data(as_text=True)
    assert all(f"<guid isPermaLink=\"false\">{g}</guid>" in xml for g in per_guid)
