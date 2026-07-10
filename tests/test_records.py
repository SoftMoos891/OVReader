from datetime import date

from app import records


def test_find_records_only_returns_cancellations(client, temp_db):
    """records.find_records() is bewust versmald tot alleen uitval-records
    (geen 'op tijd'-scan meer over trip_delays, zie app/records.py)."""
    from app import server

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    for i in range(60):
        conn.execute(
            "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, ?, 'TESTROUTE')",
            (today, f"ran{i}"),
        )
    for i in range(15):
        conn.execute(
            """INSERT INTO trip_cancellations
               (trip_id, service_date, route_id, start_time, first_seen, last_seen)
               VALUES (?, ?, 'TESTROUTE', '08:00:00', 0, 0)""",
            (f"canceled{i}", today),
        )
    conn.commit()
    conn.close()

    conn = temp_db.get_conn()
    result, thresholds = records.find_records(conn, server._index)
    conn.close()

    assert set(result.keys()) == {"cancellations", "cancellations_by_operator"}
    assert set(thresholds.keys()) == {"cancellation_total", "cancellation_ran"}
    assert result["cancellations"]["worst_all_time"]["cancellation_pct"] == round(15 / 75 * 100, 1)


def test_find_records_below_threshold_is_none(client, temp_db):
    from app import server

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('c1', ?, 'TESTROUTE', '08:00:00', 0, 0)""",
        (today,),
    )
    conn.commit()
    conn.close()

    conn = temp_db.get_conn()
    result, _ = records.find_records(conn, server._index)
    conn.close()

    # Maar 1 rit totaal -- ver onder MIN_TRIPS_CANCELLATION, dus geen record.
    assert result["cancellations"]["worst_all_time"] is None


def test_find_records_splits_by_operator(client, temp_db, monkeypatch):
    """Keolis- en Transdev-uitval moeten los van elkaar herkenbaar zijn,
    niet alleen als netwerkbreed cijfer."""
    from app import server

    monkeypatch.setitem(server._index.routes, "ROUTE_K", {"short_name": "1", "operator": "Keolis"})
    monkeypatch.setitem(server._index.routes, "ROUTE_T", {"short_name": "2", "operator": "Transdev"})

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    for i in range(60):
        conn.execute(
            "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, ?, 'ROUTE_K')",
            (today, f"k-ran{i}"),
        )
    for i in range(40):
        conn.execute(
            """INSERT INTO trip_cancellations
               (trip_id, service_date, route_id, start_time, first_seen, last_seen)
               VALUES (?, ?, 'ROUTE_K', '08:00:00', 0, 0)""",
            (f"k-canceled{i}", today),
        )
    for i in range(8):
        conn.execute(
            "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, ?, 'ROUTE_T')",
            (today, f"t-ran{i}"),
        )
    conn.commit()
    conn.close()

    conn = temp_db.get_conn()
    result, _ = records.find_records(conn, server._index)
    conn.close()

    assert set(result["cancellations_by_operator"].keys()) == {"Keolis", "Transdev"}
    keolis_worst = result["cancellations_by_operator"]["Keolis"]["worst_all_time"]
    assert keolis_worst["cancellation_pct"] == round(40 / 100 * 100, 1)
    # Transdev had die dag maar 8 ritten totaal -- ver onder
    # MIN_TRIPS_CANCELLATION (20), dus geen "slechtste dag"-record, maar mag
    # niet ontbreken of de Keolis-cijfers besmetten.
    assert result["cancellations_by_operator"]["Transdev"]["worst_all_time"] is None


def test_collector_outage_day_is_not_a_record(client, temp_db):
    """Een dag waarop de collector plat lag registreert wel de vooraf
    aangekondigde uitval maar nauwelijks gereden ritten -- die dag lijkt op
    '100% uitval' maar is een datagat en mag geen record worden, ook al haalt
    het totaal de MIN_TRIPS_CANCELLATION-drempel ruimschoots."""
    from app import server

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    for i in range(41):  # de echte 6-juli-situatie: 41 vervallen, 0 gereden
        conn.execute(
            """INSERT INTO trip_cancellations
               (trip_id, service_date, route_id, start_time, first_seen, last_seen)
               VALUES (?, ?, 'TESTROUTE', '08:00:00', 0, 0)""",
            (f"canceled{i}", today),
        )
    conn.commit()
    conn.close()

    conn = temp_db.get_conn()
    result, _ = records.find_records(conn, server._index)
    conn.close()

    assert result["cancellations"]["worst_all_time"] is None
