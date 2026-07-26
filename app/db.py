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
    active INTEGER DEFAULT 1,
    -- Officiële geldigheidsperiode uit het GTFS-RT alert zelf
    -- (active_period.start/end), los van first_seen/last_seen die alleen
    -- zeggen wanneer WIJ de melding voor het eerst/laatst zagen. Kan in de
    -- toekomst liggen (nog te beginnen werkzaamheden) of ruimer zijn dan de
    -- periode waarin de melding actief=1 stond.
    valid_from INTEGER,
    valid_until INTEGER,
    -- GTFS-RT Alert.cause (bv. ACCIDENT/POLICE_ACTIVITY/MEDICAL_EMERGENCY) --
    -- betrouwbaarder signaal voor de ernstig-badge dan tekst-keywords, als
    -- de vervoerder het veld daadwerkelijk vult (vaak UNKNOWN_CAUSE).
    cause TEXT
);

CREATE TABLE IF NOT EXISTS rail_alerts (
    alert_id TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    disruption_type TEXT,
    type_label TEXT,
    title TEXT,
    description TEXT,
    start_time TEXT,
    end_time TEXT,
    impact INTEGER,
    stations TEXT,
    active INTEGER DEFAULT 1
);

-- Los van rail_alerts zelf: die tabel krijgt alleen nieuwe/bijgewerkte rijen
-- zolang er ook daadwerkelijk een actieve NS-storing in Utrecht is, dus
-- "geen storingen al dagen" en "de NS-fetch-job is stukgelopen" zijn dan
-- niet van elkaar te onderscheiden via MAX(last_seen). Deze tabel houdt
-- apart bij wanneer de job voor het laatst succesvol resp. met een fout
-- afrondde, ongeacht of er iets te tonen was.
CREATE TABLE IF NOT EXISTS ns_fetch_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_success_at INTEGER,
    last_error_at INTEGER,
    last_error TEXT
);

-- Permanente geschiedenis van RSS-meldingen (/lite/rss.xml): items worden
-- hier één keer geschreven door de collector zodra een gebeurtenis zich
-- voordoet (uitval boven de signaleringsgrens, een nieuwe NS-storing) en
-- blijven daarna staan, ook als de situatie weer normaal is -- de feed is
-- bewust GEEN weerspiegeling van de actuele live-status, maar een log van
-- wat er is gebeurd. guid is de PRIMARY KEY, dus INSERT OR IGNORE zorgt
-- vanzelf dat eenzelfde gebeurtenis (bv. dezelfde vervoerder+dag, of
-- dezelfde NS alert_id) maar één keer wordt vastgelegd.
CREATE TABLE IF NOT EXISTS rss_feed_items (
    guid TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    pub_date INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    -- Gezet zodra de onderliggende situatie weer normaal is (uitval weer
    -- onder de grens, NS-storing/U-OV-melding niet meer actief) -- de titel
    -- krijgt dan " (voorbij)" erachter, zodat een lezer van de feed/
    -- geschiedenispagina meteen ziet dat het achterhaald is. guid/pub_date
    -- blijven ongewijzigd (geen nieuwe melding, geen re-notify).
    resolved_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rss_items_pubdate ON rss_feed_items(pub_date);

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

-- Individueel overgeslagen tussenhaltes (GTFS-RT stop_time_update met
-- schedule_relationship SKIPPED) -- los van trip_cancellations, die alleen
-- volledig geannuleerde ritten bijhoudt. Eén rij per (rit, halte, dag), dus
-- net als trip_cancellations begrensd door het daadwerkelijke aantal
-- ritten/dag i.p.v. per 30s-poll te groeien zoals trip_delays.
CREATE TABLE IF NOT EXISTS skipped_stops (
    trip_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    route_id TEXT,
    stop_id TEXT NOT NULL,
    stop_sequence INTEGER,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    PRIMARY KEY (trip_id, service_date, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_skipped_date ON skipped_stops(service_date);
CREATE INDEX IF NOT EXISTS idx_skipped_stop ON skipped_stops(stop_id);

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

    alert_cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "valid_from" not in alert_cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN valid_from INTEGER")
    if "valid_until" not in alert_cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN valid_until INTEGER")
    if "cause" not in alert_cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN cause TEXT")

    rss_item_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rss_feed_items)")}
    if "resolved_at" not in rss_item_cols:
        conn.execute("ALTER TABLE rss_feed_items ADD COLUMN resolved_at INTEGER")

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
