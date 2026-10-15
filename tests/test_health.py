import time


def test_health_no_data(client):
    data = client.get("/api/health").get_json()
    assert data["status"] == "no_data"
    assert data["components"]["vehicle_positions"]["status"] == "no_data"
    assert data["components"]["trip_delays"]["status"] == "no_data"


def test_health_ok_when_fresh(client, temp_db):
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, lat, lon)
           VALUES (?, 'v1', 't1', 'TESTROUTE', 52.0, 5.0)""",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()

    data = client.get("/api/health").get_json()
    assert data["status"] == "ok"
    assert data["components"]["vehicle_positions"]["status"] == "ok"
    assert data["components"]["vehicle_positions"]["seconds_ago"] < 5


def test_health_stale_when_last_fetch_is_old(client, temp_db):
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, lat, lon)
           VALUES (?, 'v1', 't1', 'TESTROUTE', 52.0, 5.0)""",
        (int(time.time()) - 3600,),
    )
    conn.commit()
    conn.close()

    data = client.get("/api/health").get_json()
    assert data["status"] == "stale"
    assert data["components"]["vehicle_positions"]["status"] == "stale"


# ── Geldigheid van de statische dienstregeling (feed_info.txt) ────────────
#
# Een verlopen GTFS-feed geeft stilzwijgend verkeerde ritten; de statusrij
# hierop is het enige signaal dat de nachtelijke herbouw is blijven hangen.

import json as _json
from datetime import date, timedelta

from app import server as _server


def _write_feed_info(monkeypatch, tmp_path, end_date, feed_version="9455"):
    path = tmp_path / "gtfs_feed_info.json"
    path.write_text(
        _json.dumps({"feed_version": feed_version, "feed_end_date": end_date}),
        encoding="utf-8",
    )
    monkeypatch.setattr(_server, "_GTFS_FEED_INFO_PATH", path)


def test_feed_info_ok_when_validity_runs_well_into_the_future(monkeypatch, tmp_path):
    end = date.today() + timedelta(days=120)
    _write_feed_info(monkeypatch, tmp_path, end.strftime("%Y%m%d"))

    info = _server._gtfs_feed_info()
    assert info["status"] == "ok"
    assert info["days_until_expiry"] == 120
    assert info["feed_end_date_iso"] == end.isoformat()


def test_feed_info_warns_shortly_before_expiry(monkeypatch, tmp_path):
    end = date.today() + timedelta(days=3)
    _write_feed_info(monkeypatch, tmp_path, end.strftime("%Y%m%d"))

    assert _server._gtfs_feed_info()["status"] == "stale"


def test_feed_info_warns_exactly_on_the_threshold(monkeypatch, tmp_path):
    end = date.today() + timedelta(days=_server.GTFS_FEED_EXPIRY_WARNING_DAYS)
    _write_feed_info(monkeypatch, tmp_path, end.strftime("%Y%m%d"))

    assert _server._gtfs_feed_info()["status"] == "stale"


def test_feed_info_flags_an_expired_schedule(monkeypatch, tmp_path):
    end = date.today() - timedelta(days=1)
    _write_feed_info(monkeypatch, tmp_path, end.strftime("%Y%m%d"))

    info = _server._gtfs_feed_info()
    assert info["status"] == "no_data"
    assert info["days_until_expiry"] == -1


def test_feed_info_handles_unusable_end_date(monkeypatch, tmp_path):
    _write_feed_info(monkeypatch, tmp_path, "geen-datum")

    assert _server._gtfs_feed_info()["status"] == "no_data"


def test_feed_info_absent_before_first_rebuild(monkeypatch, tmp_path):
    monkeypatch.setattr(_server, "_GTFS_FEED_INFO_PATH", tmp_path / "bestaat-niet.json")

    assert _server._gtfs_feed_info() is None


def test_feed_info_survives_a_corrupt_file(monkeypatch, tmp_path):
    path = tmp_path / "gtfs_feed_info.json"
    path.write_text("{kapot", encoding="utf-8")
    monkeypatch.setattr(_server, "_GTFS_FEED_INFO_PATH", path)

    assert _server._gtfs_feed_info() is None


# ── Lege feed 's nachts (15 okt 2026) ─────────────────────────────────────
#
# Van 02:05 tot 03:05 bevatte de OVapi-feed geen enkele rit; de collector haalde
# gewoon elke 30 s op, maar health meldde "stale" omdat er geen nieuwe rijen
# kwamen. realtime_fetch_status zegt of het ophalen zelf lukte.

def _oude_positie_en_fetchstatus(temp_db, success_ago):
    now = int(time.time())
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO vehicle_positions (fetched_at, vehicle_id, trip_id, route_id, lat, lon)
           VALUES (?, 'v1', 't1', 'TESTROUTE', 52.0, 5.0)""",
        (now - 3600,),
    )
    conn.execute("INSERT INTO realtime_fetch_status (id, last_success_at) VALUES (1, ?)", (now - success_ago,))
    conn.commit()
    conn.close()


def test_health_ok_when_feed_empty_but_fetch_succeeds(client, temp_db):
    _oude_positie_en_fetchstatus(temp_db, 10)

    data = client.get("/api/health").get_json()
    assert data["status"] == "ok"
    assert data["components"]["realtime_fetch"]["status"] == "ok"
    assert data["components"]["vehicle_positions"]["status"] == "stale"


def test_health_stale_when_fetch_itself_is_old(client, temp_db):
    _oude_positie_en_fetchstatus(temp_db, 3600)

    data = client.get("/api/health").get_json()
    assert data["status"] == "stale"
    assert data["components"]["realtime_fetch"]["status"] == "stale"


def test_lite_health_ok_when_feed_empty_but_fetch_succeeds(temp_db):
    from app import lite_server
    lite_server.app.testing = True
    _oude_positie_en_fetchstatus(temp_db, 10)

    data = lite_server.app.test_client().get("/lite/api/health").get_json()
    assert data["status"] == "ok"
    assert data["components"]["realtime_fetch"]["status"] == "ok"


def _collect_met(monkeypatch, feed_fout=None):
    from app import collector
    monkeypatch.setattr(collector, "_index", object())
    monkeypatch.setattr(collector, "fetch_vehicle_positions", lambda index: [])
    def feed():
        if feed_fout:
            raise feed_fout
        return object(), {}
    monkeypatch.setattr(collector, "fetch_trip_updates_feed", feed)
    monkeypatch.setattr(collector, "parse_trip_delays", lambda *a: [])
    monkeypatch.setattr(collector, "parse_cancellations", lambda *a: [])
    monkeypatch.setattr(collector, "count_unresolved_cancellations_by_prefix", lambda *a: {})
    monkeypatch.setattr(collector, "fetch_alerts", lambda *a, **k: [])
    collector.collect_once()


def _fetchstatus(temp_db):
    conn = temp_db.get_conn()
    try:
        return conn.execute("SELECT * FROM realtime_fetch_status WHERE id = 1").fetchone()
    finally:
        conn.close()


def test_collector_records_success_for_empty_feed(temp_db, monkeypatch):
    _collect_met(monkeypatch)

    rij = _fetchstatus(temp_db)
    assert rij["last_success_at"] >= int(time.time()) - 5
    assert rij["last_error_at"] is None


def test_collector_records_error_when_feed_fails(temp_db, monkeypatch):
    _collect_met(monkeypatch, feed_fout=RuntimeError("OVapi 503"))

    rij = _fetchstatus(temp_db)
    assert rij["last_success_at"] is None
    assert "OVapi 503" in rij["last_error"]
