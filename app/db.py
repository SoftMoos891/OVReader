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
-- Samengestelde COVERING indexes (i.p.v. losse enkelvoudige indexen op
-- fetched_at/route_id): de aggregatiequeries in server.py/records.py lezen
-- altijd route_id + arrival_delay + departure_delay samen (per route, met of
-- zonder fetched_at-filter), en de kaart-/haltezoeker-endpoints filteren op
-- trip_id. Zonder deze covering indexes moet SQLite voor elke rij nog een
-- keer de tabel raadplegen voor de niet-geindexeerde kolommen -- bij
-- miljoenen rijen trip_delays was dat het verschil tussen >20s en <2s voor
-- bijvoorbeeld /api/stats.
CREATE INDEX IF NOT EXISTS idx_td_route_covering ON trip_delays(route_id, arrival_delay, departure_delay);
CREATE INDEX IF NOT EXISTS idx_td_fetched_route_covering ON trip_delays(fetched_at, route_id, arrival_delay, departure_delay);
CREATE INDEX IF NOT EXISTS idx_td_trip_id ON trip_delays(trip_id, fetched_at);

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

-- Onthoudt tot welk tijdstip (unix-epoch, exclusief) volledig afgesloten
-- lokale dagen al zijn opgeteld in route_stats_daily/route_stats_period_daily.
-- Losstaand van RETENTION_DAYS, zodat /trends niet elke keer de volledige
-- ruwe trip_delays-tabel hoeft te scannen om dagen te kunnen tonen die nog
-- niet buiten de retentie zijn gevallen.
CREATE TABLE IF NOT EXISTS rollup_watermark (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rolled_through_epoch INTEGER NOT NULL DEFAULT 0
);
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

    # Vervangen door de covering indexes hierboven (idx_td_route_covering /
    # idx_td_fetched_route_covering dekken elke query die deze twee ook
    # konden bedienen, en dan zonder de extra tabel-lookup per rij) -- op een
    # bestaande database blijven de oude indexen anders als dode gewicht
    # meegesleept bij elke INSERT/DELETE op trip_delays.
    conn.execute("DROP INDEX IF EXISTS idx_td_fetched_at")
    conn.execute("DROP INDEX IF EXISTS idx_td_route")


def init_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = get_conn()
    # De web- en collector-service roepen init_db() allebei aan bij opstart.
    # Schema-DDL (CREATE INDEX) houdt een schrijflock vast voor de hele duur
    # van de index-build -- op een grote trip_delays-tabel kan dat ruim boven
    # de standaard busy-timeout van get_conn() (30s) duren, waardoor de
    # service die als tweede start met "database is locked" crasht i.p.v. te
    # wachten tot de eerste klaar is. Alleen voor deze eenmalige
    # opstart-migratie een veel ruimere marge; request-handling connecties
    # (get_conn() elders) houden hun kortere timeout.
    conn.execute("PRAGMA busy_timeout = 120000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Database geinitialiseerd op {DB_PATH}")
