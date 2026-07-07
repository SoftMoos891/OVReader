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


def test_cancellations_split_by_operator(client, temp_db, monkeypatch):
    from app import server

    monkeypatch.setitem(server._index.routes, "ROUTE_K", {"short_name": "1", "operator": "Keolis"})
    monkeypatch.setitem(server._index.routes, "ROUTE_T", {"short_name": "2", "operator": "Transdev"})

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

    data = client.get("/api/cancellations?range=all").get_json()

    keolis_today = next(r for r in data["daily_by_operator"]["Keolis"] if r["date"] == today)
    transdev_today = next(r for r in data["daily_by_operator"]["Transdev"] if r["date"] == today)
    assert keolis_today["canceled"] == 1
    assert keolis_today["ran"] == 1  # trip k1 liep wel gewoon, naast de vervallen k2
    assert transdev_today["canceled"] == 0
    assert transdev_today["ran"] == 1

    weekday = date.today().weekday()
    keolis_weekday = data["per_weekday_by_operator"]["Keolis"][weekday]
    assert keolis_weekday["canceled"] == 1

    keolis_hour = data["per_hour_by_operator"]["Keolis"][8]
    assert keolis_hour["canceled"] == 1

    # Transdev had die dag geen enkele vervallen rit (alleen ROUTE_T/t1, dat
    # gewoon reed) -- moet toch met het juiste 'ran'-aantal in per_operator
    # staan, niet ontbreken of met een te laag totaal berekend worden.
    per_op = {a["operator"]: a for a in data["per_operator"]}
    assert per_op["Transdev"]["canceled"] == 0
    assert per_op["Transdev"]["ran"] == 1
    assert per_op["Transdev"]["cancellation_pct"] == 0.0
    assert per_op["Keolis"]["canceled"] == 1
    assert per_op["Keolis"]["ran"] == 1
