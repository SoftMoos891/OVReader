"""Achtergrondtaak die periodiek de realtime feeds ophaalt en in SQLite opslaat."""
import os
import time
import traceback

import requests
from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .gtfs_rt import (
    UtrechtIndex, fetch_vehicle_positions, fetch_trip_updates_feed,
    parse_trip_delays, parse_cancellations, fetch_alerts,
)

FETCH_INTERVAL_SECONDS = 30
# Hoe lang trip_delays/vehicle_positions als RUWE (per-halte/per-fetch) rijen
# bewaard blijven voordat ze worden opgerold tot dagstatistieken en verwijderd
# (zie cleanup_old_data()). Was 14 dagen; op productieschaal (tientallen
# miljoenen rijen) maakte dat elke query die niet volledig buiten dit venster
# valt - inclusief de standaard "Afgelopen 14 dagen"-weergave op /trends, die
# daarmee toevallig exact samenviel - een dure scan over de volledige ruwe
# tabel i.p.v. de veel kleinere, voorgeaggregeerde rolluptabellen. 7 dagen
# halveert de ruwe tabel en laat elke query die verder terugkijkt grotendeels
# uit de rollup lezen. Kost: individuele-rit-drilldown (/api/stats/trips)
# werkt alleen nog binnen dit kortere venster.
RETENTION_DAYS = 7
# In tegenstelling tot trip_delays hierboven is dit GEEN dure tabel: hoogstens
# 1 rij per rit per dag (geen per-halte/per-fetch-detail), dus jaren aan
# geschiedenis kost nauwelijks schijfruimte. Ruim gezet zodat uitvaltrends
# over meerdere jaren zichtbaar blijven i.p.v. na ruim een jaar te verdwijnen.
CANCELLATION_HISTORY_RETENTION_DAYS = 1825  # 5 jaar
# "Op tijd" (Dienstregeling): zelfde definitie als in server.py -- tussen 2 min
# te vroeg en 3 min te laat. Buiten die band telt een rit niet meer als op tijd.
ON_TIME_MIN_DELAY = -120
ON_TIME_MAX_DELAY = 180

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
                SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS on_time_count,
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
            (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY, cutoff),
        )
        conn.execute(
            f"""
            INSERT INTO route_stats_period_daily (day, route_id, period, sample_count, on_time_count, avg_delay_seconds, max_delay_seconds)
            SELECT
                strftime('%Y-%m-%d', fetched_at, 'unixepoch', 'localtime') AS day,
                route_id,
                {db.period_hour_sql()} AS period,
                COUNT(*) AS sample_count,
                SUM(CASE WHEN COALESCE(arrival_delay, departure_delay, 0) BETWEEN ? AND ? THEN 1 ELSE 0 END) AS on_time_count,
                AVG(COALESCE(arrival_delay, departure_delay, 0)) AS avg_delay_seconds,
                MAX(COALESCE(arrival_delay, departure_delay, 0)) AS max_delay_seconds
            FROM trip_delays
            WHERE fetched_at < ?
            GROUP BY day, route_id, period
            ON CONFLICT(day, route_id, period) DO UPDATE SET
                sample_count = sample_count + excluded.sample_count,
                on_time_count = on_time_count + excluded.on_time_count,
                avg_delay_seconds = (avg_delay_seconds * sample_count + excluded.avg_delay_seconds * excluded.sample_count)
                                    / (sample_count + excluded.sample_count),
                max_delay_seconds = MAX(max_delay_seconds, excluded.max_delay_seconds)
            """,
            (ON_TIME_MIN_DELAY, ON_TIME_MAX_DELAY, cutoff),
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


def vacuum_db():
    """Geeft schijfruimte van verwijderde rijen terug aan het OS.

    SQLite hergebruikt vrijgekomen pagina's uit DELETE's vanzelf voor nieuwe
    rijen (freelist), dus bus_monitor.db groeit hierdoor niet onbeperkt door --
    dit compacteert alleen het bestand zelf terug richting zijn minimale
    omvang. Niet meer als scheduled job actief (zie start_scheduler): de
    dagelijkse VACUUM vereist tijdelijk evenveel vrije schijfruimte als de
    database groot is en faalde daardoor herhaaldelijk met "disk is full",
    wat cleanup_old_data/collect_once meesleepte in "database is locked"-
    fouten. Los, handmatig te draaien zodra er genoeg vrije schijfruimte is.
    VACUUM vereist een eigen verbinding zonder open transactie, dus niet
    hergebruiken binnen een bestaande conn.
    """
    conn = db.get_conn()
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    print("[collector] vacuum klaar")


# Nachtelijke back-up van de ONVERVANGBARE historie: de compacte tabellen
# waar de langetermijntrends (5 jaar retentie) op draaien. trip_delays/
# vehicle_positions blijven er bewust buiten -- die zijn gigantisch (GB's),
# worden toch periodiek opgerold en zijn dus vervangbaar; deze tabellen niet.
BACKUP_TABLES = [
    "trip_cancellations", "trips_ran_daily",
    "route_stats_daily", "route_stats_period_daily", "alerts",
]
BACKUP_KEEP = 7  # aantal dagelijkse back-ups dat blijft staan


def backup_history():
    """Schrijft de compacte historie-tabellen naar een gecomprimeerd
    SQLite-bestand in data/backups/ (history_YYYY-MM-DD.db.gz) en ruimt
    back-ups ouder dan BACKUP_KEEP dagen op. Lokaal beschermt dit tegen een
    kapotte database/migratie; haal het bestand ook periodiek van de server
    af (zie /api/backup/latest en DEPLOY.md) om tegen verlies van de hele
    VPS beschermd te zijn."""
    import gzip
    import shutil

    backup_dir = db.DB_PATH.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    target = backup_dir / f"history_{time.strftime('%Y-%m-%d')}.db.gz"
    tmp_db = backup_dir / "history_tmp.db"
    if tmp_db.exists():
        tmp_db.unlink()

    conn = db.get_conn()
    try:
        conn.execute("ATTACH DATABASE ? AS backup", (str(tmp_db),))
        for table in BACKUP_TABLES:
            conn.execute(f"CREATE TABLE backup.{table} AS SELECT * FROM {table}")
        conn.execute("DETACH DATABASE backup")
    finally:
        conn.close()

    with open(tmp_db, "rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    tmp_db.unlink()

    backups = sorted(backup_dir.glob("history_*.db.gz"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink()
    print(f"[collector] backup klaar: {target.name} ({target.stat().st_size // 1024} KB)")


WEB_BASE_URL = "http://127.0.0.1:5151"
# /trends draait sinds de vereenvoudiging (geen 'op tijd'-data meer) alleen
# nog op /api/records (dagelijks gecachet, zie _cached_daily() in server.py)
# en /api/cancellations (ongecachet maar intrinsiek goedkoop: trip_cancellations/
# trips_ran_daily zijn compacte tabellen, geen scan over trip_delays) -- dus
# alleen /api/records is de moeite van voorverwarmen waard.
TRENDS_WARMUP_PATHS = [
    "/api/records",
]


def warm_trends_cache():
    """Belast 's nachts alvast de zware /trends-aggregaties voor (zie
    _cached_daily() in app/server.py, ververst om 03:30), zodat de eerste
    bezoeker van de dag niet zelf hoeft te wachten. Draait als gewone HTTP-
    requests naar de lokale webservice i.p.v. de queries hier zelf uit te
    voeren, want de response-cache leeft in dat (aparte) proces. Met 2
    gunicorn-workers heeft elk zijn eigen cache -- twee pogingen per pad
    vergroot de kans dat beide warm starten, een garantie is dat niet."""
    auth = None
    password = os.environ.get("BUS_MONITOR_PASSWORD")
    if password:
        auth = (os.environ.get("BUS_MONITOR_USER", "admin"), password)
    for path in TRENDS_WARMUP_PATHS:
        for _ in range(2):
            try:
                requests.get(f"{WEB_BASE_URL}{path}", auth=auth, timeout=120)
            except requests.RequestException:
                print(f"[collector] voorverwarmen {path} mislukt:")
                traceback.print_exc()
    print("[collector] /trends voorverwarmd")


def start_scheduler():
    db.init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(collect_once, "interval", seconds=FETCH_INTERVAL_SECONDS, id="collect", max_instances=1)
    scheduler.add_job(cleanup_old_data, "interval", hours=6, id="cleanup", max_instances=1)
    # vacuum_db() is bewust niet meer gescheduled: de dagelijkse VACUUM
    # vereist tijdelijk ~evenveel vrije schijfruimte als de database groot
    # is en faalde daardoor elke dag met "disk is full", wat cleanup/collect
    # meesleepte in "database is locked"-fouten. Handmatig te draaien zodra
    # er genoeg vrije schijfruimte is.
    # 5 minuten na de cache-boundary in server.py (TRENDS_REFRESH_HOUR:MINUTE
    # = 03:30) zodat er geen twijfel is of die grens al gepasseerd is.
    scheduler.add_job(warm_trends_cache, "cron", hour=3, minute=35, id="warm_trends", max_instances=1)
    scheduler.add_job(backup_history, "cron", hour=4, minute=15, id="backup", max_instances=1)
    scheduler.start()
    # Meteen een eerste keer ophalen bij opstarten, niet pas na 30s wachten.
    collect_once()
    return scheduler
