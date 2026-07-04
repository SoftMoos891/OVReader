"""
Bouwt een gefilterde index van GTFS statische data voor U-OV: de concessie
voor het busvervoer in de provincie Utrecht, uitgevoerd door Keolis en
Transdev onder de gezamenlijke merknaam U-OV (agency_id "UOV" in de
landelijke feed).

Downloadt (indien nodig) de landelijke statische GTFS-feed van OVapi, filtert
routes.txt op agency_id "UOV" en route_type bus, en leidt daaruit de
bijbehorende trips/haltes af. Resultaat wordt weggeschreven als compacte
JSON-bestanden die de realtime-fetchers gebruiken om alleen U-OV-data te
verwerken (geen Qbuzz/Connexxion/Arriva/GVB/NS-bussen die toevallig de
provincie doorkruisen).

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

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GTFS_ZIP_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
GTFS_ZIP_PATH = DATA_DIR / "gtfs-nl.zip"
OUT_STOPS = DATA_DIR / "utrecht_stops.json"
OUT_ROUTES = DATA_DIR / "utrecht_routes.json"
OUT_TRIPS = DATA_DIR / "utrecht_trips.json"

TARGET_AGENCY_ID = "UOV"
BUS_ROUTE_TYPE = "3"

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


def find_uov_bus_routes(zf):
    """Filtert routes.txt op de U-OV-concessie, alleen bus (geen sneltram)."""
    routes = {}
    with zf.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row.get("agency_id") == TARGET_AGENCY_ID and row.get("route_type") == BUS_ROUTE_TYPE:
                routes[row["route_id"]] = {
                    "agency_id": row["agency_id"],
                    "short_name": row.get("route_short_name", ""),
                    "long_name": row.get("route_long_name", ""),
                    "route_type": row.get("route_type", ""),
                }
    return routes


def load_agency_name(zf):
    with zf.open("agency.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["agency_id"] == TARGET_AGENCY_ID:
                return row.get("agency_name", TARGET_AGENCY_ID)
    return TARGET_AGENCY_ID


def find_trips_for_routes(zf, route_ids):
    """Filtert trips.txt op de gevonden U-OV buslijnen."""
    trip_to_route = {}
    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["route_id"] in route_ids:
                trip_to_route[row["trip_id"]] = row["route_id"]
    return trip_to_route


def find_stops_for_trips(zf, trip_ids):
    """Streamt stop_times.txt (groot bestand) om te bepalen welke haltes deze
    U-OV trips aandoen. Gebruikt door de alerts-fallback (storingen die enkel
    een halte noemen, geen lijn/route)."""
    stop_ids = set()
    with zf.open("stop_times.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for i, row in enumerate(reader):
            if row["trip_id"] in trip_ids:
                stop_ids.add(row["stop_id"])
            if i % 2_000_000 == 0 and i:
                log(f"  ...{i:,} stop_times regels verwerkt, {len(stop_ids):,} haltes gevonden")
    return stop_ids


def load_stop_info(zf, stop_ids):
    stops = {}
    with zf.open("stops.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["stop_id"] in stop_ids:
                try:
                    lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
                except (KeyError, ValueError):
                    continue
                stops[row["stop_id"]] = {"name": row.get("stop_name", ""), "lat": lat, "lon": lon}
    return stops


def main():
    DATA_DIR.mkdir(exist_ok=True)
    download_gtfs_zip()

    with zipfile.ZipFile(GTFS_ZIP_PATH) as zf:
        log(f"Filter routes.txt op agency_id={TARGET_AGENCY_ID!r} en bus (route_type=3)...")
        routes = find_uov_bus_routes(zf)
        agency_name = load_agency_name(zf)
        for r in routes.values():
            r["agency_name"] = agency_name
        log(f"{len(routes)} U-OV buslijnen gevonden.")

        trip_to_route = find_trips_for_routes(zf, set(routes))
        log(f"{len(trip_to_route):,} trips gevonden voor deze lijnen.")

        log("Bepaal haltes die deze trips aandoen (doorzoekt een groot bestand, even geduld)...")
        stop_ids = find_stops_for_trips(zf, set(trip_to_route))
        stop_info = load_stop_info(zf, stop_ids)
        log(f"{len(stop_info):,} haltes gevonden.")

    line_names = sorted({r["short_name"] for r in routes.values()}, key=lambda s: (len(s), s))
    log(f"Lijnnummers ({len(line_names)}): {', '.join(line_names)}")

    OUT_STOPS.write_text(json.dumps(stop_info, ensure_ascii=False), encoding="utf-8")
    OUT_ROUTES.write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
    OUT_TRIPS.write_text(json.dumps(trip_to_route, ensure_ascii=False), encoding="utf-8")
    log(f"Weggeschreven: {OUT_STOPS.name}, {OUT_ROUTES.name}, {OUT_TRIPS.name}")


if __name__ == "__main__":
    main()
