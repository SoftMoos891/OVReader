from datetime import date, datetime


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

    week_key = f"{date.today().isocalendar()[0]}-W{date.today().isocalendar()[1]:02d}"
    keolis_week = next(w for w in data["per_week_by_operator"]["Keolis"] if w["week"] == week_key)
    assert keolis_week["canceled"] == 1
    assert keolis_week["ran"] == 1
    transdev_week = next(w for w in data["per_week_by_operator"]["Transdev"] if w["week"] == week_key)
    assert transdev_week["canceled"] == 0
    assert transdev_week["ran"] == 1


def test_up_to_now_excludes_future_start_times_for_today(client, temp_db, monkeypatch):
    """?up_to_now=1 moet een vooraf aangekondigde uitval voor een vertrektijd
    die vandaag nog moet komen negeren, maar een allang verstreken vertrektijd
    (en alles van eerdere dagen) gewoon meetellen."""
    from app import server

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 10, 12, 0, 0)  # 'nu' = 12:00 op 10 juli 2026

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 10)

    # datetime.date/datetime.datetime zijn onveranderlijke C-types -- niet
    # rechtstreeks patchbaar, vandaar deze subclasses die alleen today()/now()
    # vastzetten en de module-naam in server.py vervangen.
    monkeypatch.setattr(server, "datetime", FixedDatetime)
    monkeypatch.setattr(server, "date", FixedDate)

    today = "2026-07-10"
    yesterday = "2026-07-09"
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('past', ?, 'TESTROUTE', '08:00:00', 0, 0)""",
        (today,),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('future', ?, 'TESTROUTE', '18:00:00', 0, 0)""",
        (today,),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('yesterday', ?, 'TESTROUTE', '18:00:00', 0, 0)""",
        (yesterday,),
    )
    conn.commit()
    conn.close()

    whole_day = client.get("/api/cancellations?range=today&up_to_now=0").get_json()
    assert whole_day["total_canceled"] == 2  # past + future, allebei vandaag

    up_to_now = client.get("/api/cancellations?range=today&up_to_now=1").get_json()
    assert up_to_now["up_to_now"] is True
    assert up_to_now["total_canceled"] == 1  # alleen 'past'

    # Gisteren is sowieso al helemaal voorbij -- up_to_now maakt daar geen verschil.
    up_to_now_week = client.get("/api/cancellations?range=week&up_to_now=1").get_json()
    assert up_to_now_week["total_canceled"] == 2  # 'past' (vandaag) + 'yesterday'
