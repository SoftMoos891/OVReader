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


def test_stats_telt_opgerolde_dagen_niet_dubbel(client, temp_db):
    """6 okt 2026: ruwe rijen van een dag die al in route_stats_daily staat (vóór de
    rollup-watermark) tellen niet nog eens mee; alleen ruwe rijen vanaf de watermark."""
    import datetime
    vandaag = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp())
    gisteren = vandaag - 86400
    conn = temp_db.get_conn()
    for i in range(3):   # gisteren, al opgerold
        _insert_delay(conn, gisteren + 3600 + i, f"g{i}", "TESTROUTE", 0)
    for i in range(2):   # vandaag, nog ruw
        _insert_delay(conn, vandaag + 60 + i, f"v{i}", "TESTROUTE", 0)
    conn.execute(
        "INSERT INTO route_stats_daily (day, route_id, sample_count, on_time_count, avg_delay_seconds, max_delay_seconds) "
        "VALUES (?, 'TESTROUTE', 3, 3, 0, 0)",
        (time.strftime("%Y-%m-%d", time.localtime(gisteren)),),
    )
    conn.execute("INSERT INTO rollup_watermark (id, rolled_through_epoch) VALUES (1, ?)", (vandaag,))
    conn.commit()
    conn.close()

    data = client.get("/api/stats").get_json()
    route = next(r for r in data["per_route"] if r["route_id"] == "TESTROUTE")
    assert route["sample_count"] == 5, route   # 3 opgerold + 2 ruw, niet 3 + 5


def test_route_covering_index_alleen_s_nachts_weg(temp_db, monkeypatch):
    """De dure index staat niet meer in het schema en verdwijnt alleen in het nachtvenster."""
    from app import db
    assert "CREATE INDEX IF NOT EXISTS idx_td_route_covering" not in db.SCHEMA
    conn = db.get_conn()
    conn.execute("CREATE INDEX idx_td_route_covering ON trip_delays(route_id, arrival_delay, departure_delay)")
    namen = lambda: {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    monkeypatch.setattr(db, "_nachtvenster", lambda: False)
    db._migrate(conn)
    assert "idx_td_route_covering" in namen(), "overdag niet weggooien"
    monkeypatch.setattr(db, "_nachtvenster", lambda: True)
    db._migrate(conn)
    assert "idx_td_route_covering" not in namen()
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1   # NORMAL
    conn.close()
