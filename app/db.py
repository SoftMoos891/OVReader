"""SQLite opslag voor realtime busdata van de provincie Utrecht."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bus_monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at INTEGER NOT NULL,
    vehicle_id TEXT,
    trip_id TEXT,
    route_id TEXT,
    lat REAL,
    lon REAL,
    speed REAL,
    bearing REAL,
    delay_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS idx_vp_fetched_at ON vehicle_positions(fetched_at);
CREATE INDEX IF NOT EXISTS idx_vp_route ON vehicle_positions(route_id);

CREATE TABLE IF NOT EXISTS trip_delays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at INTEGER NOT NULL,
    trip_id TEXT,
    route_id TEXT,
    stop_id TEXT,
    stop_sequence INTEGER,
    arrival_delay INTEGER,
    departure_delay INTEGER
);
CREATE INDEX IF NOT EXISTS idx_td_fetched_at ON trip_delays(fetched_at);
CREATE INDEX IF NOT EXISTS idx_td_route ON trip_delays(route_id);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    route_ids TEXT,
    header TEXT,
    description TEXT,
    effect TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS route_stats_daily (
    day TEXT NOT NULL,
    route_id TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    on_time_count INTEGER NOT NULL,
    avg_delay_seconds REAL NOT NULL,
    max_delay_seconds INTEGER NOT NULL,
    PRIMARY KEY (day, route_id)
);

CREATE TABLE IF NOT EXISTS route_stats_period_daily (
    day TEXT NOT NULL,
    route_id TEXT NOT NULL,
    period TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    on_time_count INTEGER NOT NULL,
    avg_delay_seconds REAL NOT NULL,
    max_delay_seconds INTEGER NOT NULL,
    PRIMARY KEY (day, route_id, period)
);

CREATE TABLE IF NOT EXISTS trip_cancellations (
    trip_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    route_id TEXT,
    start_time TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (trip_id, service_date)
);
CREATE INDEX IF NOT EXISTS idx_cancel_date ON trip_cancellations(service_date);
CREATE INDEX IF NOT EXISTS idx_cancel_route ON trip_cancellations(route_id);

CREATE TABLE IF NOT EXISTS trips_ran_daily (
    service_date TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    route_id TEXT,
    PRIMARY KEY (service_date, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_ran_date ON trips_ran_daily(service_date);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# Spitsuren (lokale tijd) voor de piek/dal-analyse: 07:00-08:59 en 16:00-17:59.
PEAK_HOURS = {7, 8, 16, 17}


def period_hour_sql(fetched_at_expr="fetched_at"):
    """SQL-CASE-expressie die 'peak'/'offpeak' teruggeeft o.b.v. het lokale uur
    van fetched_at (unix-epoch seconden), consistent met PEAK_HOURS."""
    hours = ",".join(str(h) for h in sorted(PEAK_HOURS))
    return (
        f"CASE WHEN CAST(strftime('%H', {fetched_at_expr}, 'unixepoch', 'localtime') AS INTEGER) "
        f"IN ({hours}) THEN 'peak' ELSE 'offpeak' END"
    )


def _migrate(conn):
    """Voegt kolommen toe die in een eerdere versie van het schema ontbraken,
    zonder bestaande data te verliezen."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trip_cancellations)")}
    if "start_time" not in cols:
        conn.execute("ALTER TABLE trip_cancellations ADD COLUMN start_time TEXT")


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database geinitialiseerd op {DB_PATH}")
