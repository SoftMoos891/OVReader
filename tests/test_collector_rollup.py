import time

from app import collector


def test_rollup_then_cleanup_moves_old_delays_to_daily_stats(temp_db):
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

    # rollup_completed_days() telt de afgesloten dag op (los van RETENTION_DAYS);
    # cleanup_old_data() mag de ruwe rijen daarna pas verwijderen.
    collector.rollup_completed_days()
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


def test_cleanup_without_rollup_never_deletes_unrolled_raw_rows(temp_db):
    """cleanup_old_data() mag nooit ruwe rijen verwijderen die nog niet door
    rollup_completed_days() zijn meegeteld -- anders verdwijnt data uit
    /trends zonder ooit in route_stats_daily te hebben gezeten."""
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

    collector.cleanup_old_data()  # geen voorafgaande rollup

    conn = temp_db.get_conn()
    remaining_raw = conn.execute("SELECT COUNT(*) AS c FROM trip_delays").fetchone()["c"]
    conn.close()

    assert remaining_raw == 1


def test_rollup_merges_into_existing_daily_stats_row(temp_db):
    """Als route_stats_daily voor een dag/lijn al een rij heeft (bv. van een
    eerdere rollup-run), moet een nieuwe rollup optellen, niet overschrijven."""
    old_ts = int(time.time()) - (collector.RETENTION_DAYS + 1) * 86400
    day = time.strftime("%Y-%m-%d", time.localtime(old_ts))
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO route_stats_daily
           (day, route_id, sample_count, on_time_count, avg_delay_seconds, max_delay_seconds)
           VALUES (?, 'TESTROUTE', 1, 1, 0.0, 0)""",
        (day,),
    )
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, 't2', 'TESTROUTE', 'S1', 1, 600, NULL)""",
        (old_ts,),
    )
    conn.commit()
    conn.close()

    collector.rollup_completed_days()

    conn = temp_db.get_conn()
    row = conn.execute(
        "SELECT * FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()
    conn.close()

    assert row["sample_count"] == 2
    assert row["max_delay_seconds"] == 600
    assert row["avg_delay_seconds"] == 300.0  # (1*0 + 1*600) / 2


def test_rollup_is_idempotent_within_the_same_day(temp_db):
    """Een tweede rollup-run dezelfde dag mag niets dubbel optellen (de
    watermark voorkomt dat dezelfde ruwe rijen twee keer meetellen)."""
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

    collector.rollup_completed_days()
    collector.rollup_completed_days()

    conn = temp_db.get_conn()
    row = conn.execute(
        "SELECT * FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()
    conn.close()

    assert row["sample_count"] == 1
