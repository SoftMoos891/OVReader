import datetime as dt
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
    rollup_completed_days() zijn opgeteld -- anders verdwijnt data zonder
    ooit in route_stats_daily terecht te zijn gekomen."""
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

    collector.cleanup_old_data()  # geen rollup vooraf -- watermark staat nog op 0

    conn = temp_db.get_conn()
    remaining_raw = conn.execute("SELECT COUNT(*) AS c FROM trip_delays").fetchone()["c"]
    rolled = conn.execute("SELECT COUNT(*) AS c FROM route_stats_daily").fetchone()["c"]
    conn.close()

    assert remaining_raw == 1
    assert rolled == 0


def test_rollup_merges_local_day_split_across_two_chunk_boundaries(temp_db):
    """Elke chunk is een vast blok van 86400 seconden vanaf de vorige
    watermark -- niet per se uitgelijnd op lokale middernacht (die
    uitlijning hangt af van het tijdstip van de oudste ruwe rij, ofwel de
    vorige dag_end). Eén lokale kalenderdag kan daardoor over twee
    opeenvolgende chunks verdeeld raken: het eind van chunk 1 en het begin
    van chunk 2 vallen dan allebei binnen diezelfde dag. Dit test dat zo'n
    gesplitste dag correct wordt samengevoegd via de ON CONFLICT-upsert,
    i.p.v. dat de tweede chunk de bijdrage van de eerste overschrijft."""
    anchor_local = dt.datetime.combine(
        dt.date.today() - dt.timedelta(days=3), dt.time(8, 0)
    )
    anchor_ts = int(anchor_local.timestamp())
    shared_date = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    # Chunk 1 = [anchor_ts, anchor_ts + 86400) -> bevat shared_date 00:00-08:00.
    in_chunk1_ts = int(dt.datetime.combine(
        dt.date.today() - dt.timedelta(days=2), dt.time(2, 0)
    ).timestamp())
    # Chunk 2 = [anchor_ts + 86400, ...) -> bevat shared_date 08:00-24:00.
    in_chunk2_ts = int(dt.datetime.combine(
        dt.date.today() - dt.timedelta(days=2), dt.time(14, 0)
    ).timestamp())
    assert anchor_ts < in_chunk1_ts < anchor_ts + 86400 <= in_chunk2_ts

    conn = temp_db.get_conn()
    for ts, trip in [(anchor_ts, "anchor"), (in_chunk1_ts, "t1"), (in_chunk2_ts, "t2")]:
        conn.execute(
            """INSERT INTO trip_delays
               (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
               VALUES (?, ?, 'TESTROUTE', 'S1', 1, 0, NULL)""",
            (ts, trip),
        )
    conn.commit()
    conn.close()

    collector.rollup_completed_days()

    conn = temp_db.get_conn()
    row = conn.execute(
        "SELECT * FROM route_stats_daily WHERE route_id = 'TESTROUTE' AND day = ?",
        (shared_date,),
    ).fetchone()
    conn.close()

    # Beide rijen (t1 uit chunk 1, t2 uit chunk 2) horen samengeteld te zijn
    # voor shared_date, niet elkaar te hebben overschreven.
    assert row is not None
    assert row["sample_count"] == 2


def test_rollup_processes_backlog_one_day_per_transaction(temp_db, capsys):
    """Kernpunt van de chunked-aanpak: bij een grote achterstand (meerdere
    dagen ruwe data zonder watermark) moet elke dag een EIGEN commit krijgen
    -- niet alles in een enkele transactie, zoals de eerdere, live vastgelopen
    versie deed. rollup_completed_days() logt na elke geslaagde
    dag-transactie/commit "dag-rollup klaar"; het aantal regels is dus
    rechtstreeks het aantal commits."""
    now = int(time.time())
    conn = temp_db.get_conn()
    for days_ago in range(1, 4):
        ts = now - days_ago * 86400
        conn.execute(
            """INSERT INTO trip_delays
               (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
               VALUES (?, ?, 'TESTROUTE', 'S1', 1, 0, NULL)""",
            (ts, f"t{days_ago}"),
        )
    conn.commit()
    conn.close()

    capsys.readouterr()  # ruim eventuele eerdere output op
    collector.rollup_completed_days()
    output = capsys.readouterr().out

    # Minstens 3 afzonderlijke commits (een per afgesloten dag) i.p.v. 1.
    assert output.count("dag-rollup klaar") >= 3

    conn = temp_db.get_conn()
    total = conn.execute(
        "SELECT SUM(sample_count) AS c FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()["c"]
    conn.close()
    assert total == 3


def test_rollup_bootstrap_without_watermark_starts_at_oldest_raw_row(temp_db, capsys):
    """Zonder bestaande watermark-rij (verse deploy/migratie) mag de lus NIET
    vanaf epoch 0 beginnen -- dat zou door tientallen jaren lege dagen heen
    moeten stappen voordat er data wordt bereikt. In plaats daarvan moet hij
    beginnen bij de daadwerkelijk oudste ruwe rij: hier gezet op
    RETENTION_DAYS + 2 dagen oud (bv. omdat opschoning een tijdje heeft
    stilgelegen), ruim voorbij een vaste RETENTION_DAYS-ondergrens. Toetst
    zowel dat die data wordt meegenomen (niet overgeslagen) als dat het
    aantal iteraties beperkt blijft tot de werkelijke achterstand, niet tot
    decennia sinds 1970."""
    now = int(time.time())
    oldest_ts = now - (collector.RETENTION_DAYS + 2) * 86400
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO trip_delays
           (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
           VALUES (?, 't1', 'TESTROUTE', 'S1', 1, 0, NULL)""",
        (oldest_ts,),
    )
    conn.commit()
    conn.close()

    capsys.readouterr()
    collector.rollup_completed_days()
    output = capsys.readouterr().out

    # Begrensd tot de werkelijke achterstand (~RETENTION_DAYS + 2 dagen),
    # niet tot tienduizenden dagen sinds epoch 0.
    assert 0 < output.count("dag-rollup klaar") <= collector.RETENTION_DAYS + 3

    conn = temp_db.get_conn()
    row = conn.execute(
        "SELECT * FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()
    watermark = conn.execute(
        "SELECT rolled_through_epoch FROM rollup_watermark WHERE id = 1"
    ).fetchone()["rolled_through_epoch"]
    conn.close()

    assert row is not None  # de oude rij is niet overgeslagen
    assert row["sample_count"] == 1
    assert watermark == collector._today_start_epoch()


def test_rollup_resumes_from_watermark_after_interruption(temp_db):
    """Als rollup_completed_days() halverwege de achterstand wordt
    onderbroken (bv. door een herstart), moet een volgende aanroep verder
    gaan vanaf de laatst gecommitte watermark i.p.v. van voren af aan te
    beginnen -- en mag dezelfde dag niet dubbel geteld worden."""
    now = int(time.time())
    conn = temp_db.get_conn()
    for days_ago in range(1, 4):
        ts = now - days_ago * 86400
        conn.execute(
            """INSERT INTO trip_delays
               (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
               VALUES (?, ?, 'TESTROUTE', 'S1', 1, 0, NULL)""",
            (ts, f"t{days_ago}"),
        )
    conn.commit()
    conn.close()

    collector.rollup_completed_days()  # rolt in een keer door tot vandaag

    conn = temp_db.get_conn()
    watermark_after_full_run = conn.execute(
        "SELECT rolled_through_epoch FROM rollup_watermark WHERE id = 1"
    ).fetchone()["rolled_through_epoch"]
    conn.close()
    assert watermark_after_full_run == collector._today_start_epoch()

    # Nogmaals aanroepen (zoals de uurlijkse job) mag niets dubbel tellen.
    collector.rollup_completed_days()

    conn = temp_db.get_conn()
    total = conn.execute(
        "SELECT SUM(sample_count) AS c FROM route_stats_daily WHERE route_id = 'TESTROUTE'"
    ).fetchone()["c"]
    conn.close()
    assert total == 3


def test_rollup_yields_lock_between_days_for_concurrent_writer(temp_db, monkeypatch):
    """Reproduceert het productie-incident in het klein: tijdens een lopende
    rollup over meerdere dagen moet een andere connectie (zoals collect_once)
    tussen de dagen door kunnen schrijven, i.p.v. de hele rollup te moeten
    uitzitten. Dit was precies waarom de niet-chunked versie vastliep: die
    hield de schrijflock voor de VOLLEDIGE achterstand in een enkele
    transactie vast.

    Om dit deterministisch te toetsen (niet op toeval/timing te varen) wordt
    db.get_conn() zo geïnstrumenteerd dat vlak vóór de TWEEDE chunk-iteratie
    (dus na de commit van chunk 1, vóór chunk 2 begint) gewacht wordt tot een
    aparte schrijver-thread zijn insert+commit heeft voltooid. Lukt dat niet
    binnen de timeout, dan zat de lock nog vast en faalt de test."""
    import threading

    now = int(time.time())
    conn = temp_db.get_conn()
    for days_ago in range(1, 4):
        ts = now - days_ago * 86400
        conn.execute(
            """INSERT INTO trip_delays
               (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
               VALUES (?, ?, 'TESTROUTE', 'S1', 1, 0, NULL)""",
            (ts, f"t{days_ago}"),
        )
    conn.commit()
    conn.close()

    real_get_conn = collector.db.get_conn
    call_count = {"n": 0}
    writer_done = threading.Event()

    def instrumented_get_conn():
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Precies het venster tussen chunk 1's commit en chunk 2's start.
            assert writer_done.wait(timeout=5), (
                "schrijver kon niet voltooien tussen chunk 1 en chunk 2 -- "
                "de lock lijkt nog vastgehouden te worden"
            )
        return real_get_conn()

    monkeypatch.setattr(collector.db, "get_conn", instrumented_get_conn)

    def writer():
        c = temp_db.get_conn()
        c.execute(
            """INSERT INTO vehicle_positions
               (fetched_at, vehicle_id, trip_id, route_id, lat, lon, speed, bearing)
               VALUES (?, 'v1', 't1', 'TESTROUTE', 0, 0, 0, 0)""",
            (int(time.time()),),
        )
        c.commit()
        c.close()
        writer_done.set()

    threading.Thread(target=writer).start()
    collector.rollup_completed_days()

    assert writer_done.is_set()
    conn = temp_db.get_conn()
    count = conn.execute("SELECT COUNT(*) AS c FROM vehicle_positions").fetchone()["c"]
    conn.close()
    assert count == 1
