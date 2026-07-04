"""
Bouwt een gefilterde index van GTFS statische data voor de provincie Utrecht.

Downloadt (indien nodig) de landelijke statische GTFS-feed van OVapi, bepaalt
welke stops binnen de provinciegrens van Utrecht liggen, en leidt daaruit af
welke trips/routes/agencies daadwerkelijk relevant zijn. Resultaat wordt
weggeschreven als compacte JSON-bestanden die de realtime-fetchers gebruiken
om ruis van andere provincies/operators weg te filteren.

Herhaal dit script periodiek (bv. wekelijks) om dienstregelingswijzigingen
bij te houden; de statische feed zelf verandert niet elke minuut.
"""
import csv
import io
import json
import sys
import zipfile
from pathlib import Path

import requests
from shapely.geometry import shape, Point

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GTFS_ZIP_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
GTFS_ZIP_PATH = DATA_DIR / "gtfs-nl.zip"
PROVINCES_PATH = DATA_DIR / "provincies.geojson"
OUT_STOPS = DATA_DIR / "utrecht_stops.json"
OUT_ROUTES = DATA_DIR / "utrecht_routes.json"
OUT_TRIPS = DATA_DIR / "utrecht_trips.json"

csv.field_size_limit(sys.maxsize)


def log(msg):
    print(f"[build_static_index] {msg}", flush=True)


def download_gtfs_zip():
    if GTFS_ZIP_PATH.exists():
        log(f"Gebruik gecachte GTFS-zip ({GTFS_ZIP_PATH.stat().st_size / 1e6:.0f} MB)")
        return
    log("Download landelijke statische GTFS-feed (~230 MB)...")
    with requests.get(GTFS_ZIP_URL, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(GTFS_ZIP_PATH, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    log("Download klaar.")


def load_utrecht_polygon():
    provinces = json.loads(PROVINCES_PATH.read_text(encoding="utf-8"))
    for feature in provinces["features"]:
        if feature["properties"]["statnaam"] == "Utrecht":
            return shape(feature["geometry"])
    raise RuntimeError("Provincie Utrecht niet gevonden in provincies.geojson")


def find_utrecht_stops(zf, polygon):
    """Geeft set van stop_id's terug die binnen de provinciegrens vallen."""
    # Kleine marge (buffer) zodat haltes vlak over de grens niet wegvallen.
    polygon_buffered = polygon.buffer(0.01)  # ~1 km in graden
    stop_ids = set()
    stop_info = {}
    with zf.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            try:
                lat = float(row["stop_lat"])
                lon = float(row["stop_lon"])
            except (KeyError, ValueError):
                continue
            if polygon_buffered.contains(Point(lon, lat)):
                stop_ids.add(row["stop_id"])
                stop_info[row["stop_id"]] = {
                    "name": row.get("stop_name", ""),
                    "lat": lat,
                    "lon": lon,
                }
    return stop_ids, stop_info


def find_trips_for_stops(zf, stop_ids):
    """Streamt stop_times.txt (groot bestand) en geeft trip_id's terug die
    minstens één halte binnen Utrecht aandoen."""
    trip_ids = set()
    with zf.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for i, row in enumerate(reader):
            if row["stop_id"] in stop_ids:
                trip_ids.add(row["trip_id"])
            if i % 2_000_000 == 0 and i:
                log(f"  ...{i:,} stop_times regels verwerkt, {len(trip_ids):,} trips gevonden")
    return trip_ids


def load_trips_and_routes(zf, trip_ids):
    trip_to_route = {}
    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["trip_id"] in trip_ids:
                trip_to_route[row["trip_id"]] = row["route_id"]

    route_ids = set(trip_to_route.values())
    routes = {}
    with zf.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["route_id"] in route_ids:
                routes[row["route_id"]] = {
                    "agency_id": row.get("agency_id", ""),
                    "short_name": row.get("route_short_name", ""),
                    "long_name": row.get("route_long_name", ""),
                    "route_type": row.get("route_type", ""),
                }

    agencies = {}
    with zf.open("agency.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            agencies[row["agency_id"]] = row.get("agency_name", row["agency_id"])

    for route in routes.values():
        route["agency_name"] = agencies.get(route["agency_id"], route["agency_id"])

    # Alleen bus (GTFS route_type 3) -- dit is een busvervoer-monitor, geen
    # trein/tram/metro, ook al delen die soms dezelfde operators/haltes.
    bus_route_ids = {rid for rid, r in routes.items() if r["route_type"] == "3"}
    routes = {rid: r for rid, r in routes.items() if rid in bus_route_ids}
    trip_to_route = {tid: rid for tid, rid in trip_to_route.items() if rid in bus_route_ids}

    return trip_to_route, routes


def main():
    DATA_DIR.mkdir(exist_ok=True)
    download_gtfs_zip()
    polygon = load_utrecht_polygon()

    with zipfile.ZipFile(GTFS_ZIP_PATH) as zf:
        log("Bepaal haltes binnen provincie Utrecht...")
        stop_ids, stop_info = find_utrecht_stops(zf, polygon)
        log(f"{len(stop_ids):,} haltes gevonden binnen de provinciegrens.")

        log("Bepaal trips die deze haltes aandoen (dit doorzoekt een groot bestand, even geduld)...")
        trip_ids = find_trips_for_stops(zf, stop_ids)
        log(f"{len(trip_ids):,} trips gevonden.")

        log("Koppel trips aan routes en agencies...")
        trip_to_route, routes = load_trips_and_routes(zf, trip_ids)

    agency_counts = {}
    for r in routes.values():
        agency_counts[r["agency_name"]] = agency_counts.get(r["agency_name"], 0) + 1
    log("Routes per operator binnen provincie Utrecht:")
    for name, count in sorted(agency_counts.items(), key=lambda x: -x[1]):
        log(f"  {name}: {count} routes")

    OUT_STOPS.write_text(json.dumps(stop_info, ensure_ascii=False), encoding="utf-8")
    OUT_ROUTES.write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
    OUT_TRIPS.write_text(json.dumps(trip_to_route, ensure_ascii=False), encoding="utf-8")
    log(f"Weggeschreven: {OUT_STOPS.name}, {OUT_ROUTES.name}, {OUT_TRIPS.name}")


if __name__ == "__main__":
    main()
