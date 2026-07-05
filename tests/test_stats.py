import time


def _insert_delay(conn, fetched_at, trip_id, route_id, delay):
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, ?, ?, 'S1', 1, ?, NULL)""",
        (fetched_at, trip_id, route_id, delay),
    )


def test_on_time_definition_boundaries(client, temp_db):
    """'Op tijd' (Dienstregeling): tussen 2 min te vroeg (-120s) en 3 min te
    laat (180s) inclusief. Daarbuiten telt een rit niet meer als op tijd."""
    now = int(time.time())
    cases = [
        (-121, False),  # net te vroeg
        (-120, True),   # grens: nog net op tijd
        (0, True),
        (180, True),    # grens: nog net op tijd
        (181, False),   # net te laat
    ]
    conn = temp_db.get_conn()
    for i, (delay, _) in enumerate(cases):
        _insert_delay(conn, now, f"trip{i}", "TESTROUTE", delay)
    conn.commit()
    conn.close()

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    route = next(r for r in data["per_route"] if r["route_id"] == "TESTROUTE")

    expected_on_time = sum(1 for _, on_time in cases if on_time)
    assert route["sample_count"] == len(cases)
    assert route["on_time_pct"] == round(100.0 * expected_on_time / len(cases), 1)


def test_stats_aggregates_per_operator(client, temp_db):
    now = int(time.time())
    conn = temp_db.get_conn()
    _insert_delay(conn, now, "t1", "TESTROUTE", 0)
    _insert_delay(conn, now, "t2", "TESTROUTE", 0)
    conn.commit()
    conn.close()

    data = client.get("/api/stats").get_json()
    route = next(r for r in data["per_route"] if r["route_id"] == "TESTROUTE")
    operator = next(o for o in data["per_operator"] if o["operator"] == route["operator"])

    assert operator["sample_count"] >= route["sample_count"]
