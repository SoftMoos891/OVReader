from datetime import date


def test_cancellation_percentage(client, temp_db):
    today = date.today().isoformat()
    conn = temp_db.get_conn()
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 't1', 'TESTROUTE')",
        (today,),
    )
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 't2', 'TESTROUTE')",
        (today,),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('t3', ?, 'TESTROUTE', '08:00:00', 0, 0)""",
        (today,),
    )
    conn.commit()
    conn.close()

    data = client.get("/api/cancellations?range=all").get_json()

    assert data["total_canceled"] == 1
    assert data["total_ran"] == 2
    assert data["cancellation_pct"] == round(100.0 * 1 / 3, 1)

    route = next(r for r in data["per_route"] if r["route_id"] == "TESTROUTE")
    assert route["canceled"] == 1
    assert route["ran"] == 2


def test_cancellation_percentage_zero_when_none_canceled(client, temp_db):
    today = date.today().isoformat()
    conn = temp_db.get_conn()
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 't1', 'TESTROUTE')",
        (today,),
    )
    conn.commit()
    conn.close()

    data = client.get("/api/cancellations?range=all").get_json()

    assert data["total_canceled"] == 0
    assert data["cancellation_pct"] == 0.0
    # Lijnen zonder vervallen ritten worden niet in per_route getoond.
    assert all(r["route_id"] != "TESTROUTE" for r in data["per_route"])
