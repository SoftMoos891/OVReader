"""OV-trendalarm (15 okt 2026): app/trendalarm.py en /lite/api/trendalarm."""
from datetime import date, timedelta

from app import trendalarm as ta

VANDAAG = date(2026, 10, 12)          # maandag: week = 5-11 okt, normaal = 7 sep - 4 okt


def _vul(conn, route, dag, gereden, uitgevallen, metingen=400, op_tijd=0.9, vertraging=60):
    d = dag.isoformat()
    for i in range(gereden):
        conn.execute("INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, ?, ?)", (d, f"{route}-{d}-r{i}", route))
    for i in range(uitgevallen):
        conn.execute("INSERT INTO trip_cancellations (trip_id, service_date, route_id, start_time, first_seen, last_seen) VALUES (?, ?, ?, '08:00:00', 1, 1)",
                     (f"{route}-{d}-c{i}", d, route))
    conn.execute("INSERT INTO route_stats_daily (day, route_id, sample_count, on_time_count, avg_delay_seconds, max_delay_seconds) VALUES (?, ?, ?, ?, ?, 0)",
                 (d, route, metingen, int(metingen * op_tijd), vertraging))


def _maak(temp_db, week_uitval, week_op_tijd, rare_dag=None):
    conn = temp_db.get_conn()
    for n in range(1, 36):
        dag = VANDAAG - timedelta(days=n)
        in_week = n <= 7
        u = (week_uitval if in_week else 1)
        if rare_dag is not None and n == rare_dag:
            u = 60
        _vul(conn, "R1", dag, 40, u, op_tijd=(week_op_tijd if in_week else 0.9))
        _vul(conn, "R2", dag, 40, 1)
    conn.commit()
    return conn


def test_lijn_met_een_week_meer_uitval_en_slechtere_stiptheid(temp_db):
    conn = _maak(temp_db, week_uitval=6, week_op_tijd=0.7)
    r = ta.trendalarm(conn, VANDAAG)
    assert r["week"] == ["2026-10-05", "2026-10-11"]
    assert [l["route_id"] for l in r["lijnen"]] == ["R1"]
    l = r["lijnen"][0]
    assert l["redenen"] == ["uitval", "stiptheid"] and l["uitval"]["slechte_dagen"] == 7 and l["stiptheid"]["week_pct"] == 70.0


def test_een_rare_dag_geeft_geen_alarm(temp_db):
    # Op één dag 60 "uitgevallen" ritten (zoals de OVapi-hapering van 14 okt), de rest normaal.
    conn = _maak(temp_db, week_uitval=1, week_op_tijd=0.9, rare_dag=3)
    assert ta.trendalarm(conn, VANDAAG)["lijnen"] == []


def test_api(temp_db, monkeypatch):
    from app import lite_server
    monkeypatch.setattr(lite_server._index, "is_bus_route", lambda r: True)
    _maak(temp_db, week_uitval=6, week_op_tijd=0.9)
    lite_server._trendalarm_cache.clear()
    monkeypatch.setattr(lite_server, "date", type("D", (), {"today": staticmethod(lambda: VANDAAG)}))
    lite_server.app.testing = True
    d = lite_server.app.test_client().get("/lite/api/trendalarm").get_json()
    assert [l["route_id"] for l in d["lijnen"]] == ["R1"] and d["lijnen"][0]["redenen"] == ["uitval"]


def test_netwerkbrede_rare_dag_telt_nergens_mee(temp_db):
    # Alle lijnen op dezelfde dag massaal "uitgevallen": een datahapering, geen nieuws.
    conn = temp_db.get_conn()
    for n in range(1, 36):
        dag = VANDAAG - timedelta(days=n)
        for r in ("R1", "R2", "R3"):
            _vul(conn, r, dag, 40, 30 if n == 2 else 1)
    conn.commit()
    d = ta.trendalarm(conn, VANDAAG)
    assert d["uitgesloten_dagen"] == [(VANDAAG - timedelta(days=2)).isoformat()] and d["lijnen"] == []
