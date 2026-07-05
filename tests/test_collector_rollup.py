import time

from app import collector


def test_cleanup_rolls_up_old_delays_and_deletes_raw_rows(temp_db):
    old_ts = int(time.time()) - (collector.RETENTION_DAYS + 1) * 86400
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, 't1', 'TESTROUTE', 'S1', 1, 0, NULL)""",
        (old_ts,),
    )
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, 't2', 'TESTROUTE', 'S1', 1, 300, NULL)""",
        (old_ts,),
    )
    conn.commit()
    conn.close()

    collector.cleanup_old_data()

    conn = temp_db.get_conn()
    row = conn.execute(
        "SELECT * FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()
    remaining_raw = conn.execute("SELECT COUNT(*) AS c FROM trip_delays").fetchone()["c"]
    conn.close()

    assert row is not None
    assert row["sample_count"] == 2
    assert row["on_time_count"] == 1  # delay=0 op tijd, delay=300 (5 min) te laat
    assert row["max_delay_seconds"] == 300
    assert remaining_raw == 0


def test_cleanup_merges_into_existing_rollup_on_rerun(temp_db):
    old_ts = int(time.time()) - (collector.RETENTION_DAYS + 1) * 86400
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, 't1', 'TESTROUTE', 'S1', 1, 0, NULL)""",
        (old_ts,),
    )
    conn.commit()
    conn.close()
    collector.cleanup_old_data()

    # Nog een oude rij, apart binnengekomen -- een tweede opschoning moet
    # samenvoegen met de al opgerolde dagstatistiek, niet overschrijven.
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, 't2', 'TESTROUTE', 'S1', 1, 600, NULL)""",
        (old_ts,),
    )
    conn.commit()
    conn.close()
    collector.cleanup_old_data()

    conn = temp_db.get_conn()
    row = conn.execute(
        "SELECT * FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()
    conn.close()

    assert row["sample_count"] == 2
    assert row["max_delay_seconds"] == 600
    assert row["avg_delay_seconds"] == 300.0  # (0 + 600) / 2
