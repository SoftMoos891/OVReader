"""Achtergrondtaak die periodiek de realtime feeds ophaalt en in SQLite opslaat."""
import time
import traceback

from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .gtfs_rt import (
    UtrechtIndex, fetch_vehicle_positions, fetch_trip_updates_feed,
    parse_trip_delays, parse_cancellations, fetch_alerts,
)

FETCH_INTERVAL_SECONDS = 30
RETENTION_DAYS = 14
CANCELLATION_HISTORY_RETENTION_DAYS = 400  # ruwweg, deze tabellen zijn al compact (1 rij per rit/dag)

_index = None


def _now():
    return int(time.time())


def collect_once():
    global _index
    if _index is None:
        _index = UtrechtIndex()

    fetched_at = _now()
    conn = db.get_conn()
    try:
        try:
            positions = fetch_vehicle_positions(_index)
            conn.executemany(
                """INSERT INTO vehicle_positions
                   (fetched_at, vehicle_id, trip_id, route_id, lat, lon, speed, bearing)
                   VALUES (:fetched_at, :vehicle_id, :trip_id, :route_id, :lat, :lon, :speed, :bearing)""",
                [{**p, "fetched_at": fetched_at} for p in positions],
            )
        except Exception:
            print("[collector] fout bij ophalen vehicle positions:")
            traceback.print_exc()

        trip_updates_feed = None
        try:
            trip_updates_feed = fetch_trip_updates_feed()
        except Exception:
            print("[collector] fout bij ophalen trip-updates feed:")
            traceback.print_exc()

        try:
            delays = parse_trip_delays(trip_updates_feed, _index) if trip_updates_feed is not None else []
            conn.executemany(
                """INSERT INTO trip_delays
                   (fetched_at, trip_id, route_id, stop_id, stop_sequence, arrival_delay, departure_delay)
                   VALUES (:fetched_at, :trip_id, :route_id, :stop_id, :stop_sequence, :arrival_delay, :departure_delay)""",
                [{**d, "fetched_at": fetched_at} for d in delays],
            )
            today = time.strftime("%Y-%m-%d", time.localtime(fetched_at))
            conn.executemany(
                "INSERT OR IGNORE INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, ?, ?)",
                {(today, d["trip_id"], d["route_id"]) for d in delays},
            )
        except Exception:
            print("[collector] fout bij verwerken trip updates:")
            traceback.print_exc()

        try:
            cancellations = parse_cancellations(trip_updates_feed, _index) if trip_updates_feed is not None else []
            for c in cancellations:
                service_date = c["service_date"] or time.strftime("%Y-%m-%d", time.localtime(fetched_at))
                conn.execute(
                    """INSERT INTO trip_cancellations (trip_id, service_date, route_id, start_time, first_seen, last_seen)
                       VALUES (:trip_id, :service_date, :route_id, :start_time, :now, :now)
                       ON CONFLICT(trip_id, service_date) DO UPDATE SET
                           last_seen=:now,
                           start_time=COALESCE(trip_cancellations.start_time, :start_time)""",
                    {
                        "trip_id": c["trip_id"],
                        "service_date": service_date,
                        "route_id": c["route_id"],
                        "start_time": c["start_time"],
                        "now": fetched_at,
                    },
                )
        except Exception:
            print("[collector] fout bij ophalen cancellations:")
            traceback.print_exc()

        try:
            alerts = fetch_alerts(_index)
            seen_ids = [a["alert_id"] for a in alerts]
            for a in alerts:
                conn.execute(
                    """INSERT INTO alerts (alert_id, first_seen, last_seen, route_ids, header, description, effect, active)
                       VALUES (:alert_id, :now, :now, :route_ids, :header, :description, :effect, 1)
                       ON CONFLICT(alert_id) DO UPDATE SET
                           last_seen=:now, route_ids=:route_ids, header=:header,
                           description=:description, effect=:effect, active=1""",
                    {
                        "alert_id": a["alert_id"],
                        "now": fetched_at,
                        "route_ids": ",".join(a["route_ids"]),
                        "header": a["header"],
                        "description": a["description"],
                        "effect": a["effect"],
                    },
                )
            if seen_ids:
                placeholders = ",".join("?" * len(seen_ids))
                conn.execute(
                    f"UPDATE alerts SET active=0 WHERE active=1 AND alert_id NOT IN ({placeholders})",
                    seen_ids,
                )
            else:
                conn.execute("UPDATE alerts SET active=0 WHERE active=1")
        except Exception:
            print("[collector] fout bij ophalen alerts:")
            traceback.print_exc()

        conn.commit()
    finally:
        conn.close()

    print(f"[collector] fetch klaar op {fetched_at}")


def cleanup_old_data():
    """Rolt oude ruwe metingen op tot dagstatistieken per route en verwijdert
    daarna de ruwe rijen, zodat de database niet onbeperkt groeit."""
    cutoff = _now() - RETENTION_DAYS * 86400
    conn = db.get_conn()
    try:
        conn.execute(
            """
            INSERT INTO route_stats_daily (day, route_id, sample_count, on_time_count, avg_delay_seconds, max_delay_seconds)
            SELECT
                date(fetched_at, 'unixepoch') AS day,
                route_id,
                COUNT(*) AS sample_count,
                SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) <= 180 THEN 1 ELSE 0 END) AS on_time_count,
                AVG(COALESCE(arrival_delay, departure_delay, 0)) AS avg_delay_seconds,
                MAX(COALESCE(arrival_delay, departure_delay, 0)) AS max_delay_seconds
            FROM trip_delays
            WHERE fetched_at < ?
            GROUP BY day, route_id
            ON CONFLICT(day, route_id) DO UPDATE SET
                sample_count = sample_count + excluded.sample_count,
                on_time_count = on_time_count + excluded.on_time_count,
                avg_delay_seconds = (avg_delay_seconds * sample_count + excluded.avg_delay_seconds * excluded.sample_count)
                                    / (sample_count + excluded.sample_count),
                max_delay_seconds = MAX(max_delay_seconds, excluded.max_delay_seconds)
            """,
            (cutoff,),
        )
        conn.execute("DELETE FROM trip_delays WHERE fetched_at < ?", (cutoff,))
        conn.execute("DELETE FROM vehicle_positions WHERE fetched_at < ?", (cutoff,))

        history_cutoff_date = time.strftime(
            "%Y-%m-%d", time.localtime(_now() - CANCELLATION_HISTORY_RETENTION_DAYS * 86400)
        )
        conn.execute("DELETE FROM trip_cancellations WHERE service_date < ?", (history_cutoff_date,))
        conn.execute("DELETE FROM trips_ran_daily WHERE service_date < ?", (history_cutoff_date,))
        conn.commit()
    finally:
        conn.close()
    print(f"[collector] opschoning klaar (cutoff={cutoff})")


def start_scheduler():
    db.init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(collect_once, "interval", seconds=FETCH_INTERVAL_SECONDS, id="collect", max_instances=1)
    scheduler.add_job(cleanup_old_data, "interval", hours=6, id="cleanup", max_instances=1)
    scheduler.start()
    # Meteen een eerste keer ophalen bij opstarten, niet pas na 30s wachten.
    collect_once()
    return scheduler
