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
