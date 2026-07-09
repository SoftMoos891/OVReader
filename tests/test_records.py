from datetime import date

from app import records


def test_find_records_only_returns_cancellations(client, temp_db):
    """records.find_records() is bewust versmald tot alleen uitval-records
    (geen 'op tijd'-scan meer over trip_delays, zie app/records.py)."""
    from app import server

    today = date.today().isoformat()
    conn = temp_db.get_conn()
    for i in range(25):
        conn.execute(
            "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, ?, 'TESTROUTE')",
            (today, f"ran{i}"),
        )
    for i in range(5):
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

    assert set(result.keys()) == {"cancellations"}
    assert set(thresholds.keys()) == {"cancellation_total"}
    assert result["cancellations"]["worst_all_time"]["cancellation_pct"] == round(5 / 30 * 100, 1)


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
