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

from .concession_mapping import classify_operator, UNKNOWN

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
GTFS_ZIP_URL = "https://gtfs.ovapi.nl/nl/gtfs-nl.zip"
GTFS_ZIP_PATH = DATA_DIR / "gtfs-nl.zip"
OUT_STOPS = DATA_DIR / "utrecht_stops.json"
OUT_ROUTES = DATA_DIR / "utrecht_routes.json"
OUT_TRIPS = DATA_DIR / "utrecht_trips.json"
OUT_CALENDAR = DATA_DIR / "utrecht_calendar.json"
OUT_TRIP_META = DATA_DIR / "utrecht_trip_meta.json"
OUT_STOP_TIMES = DATA_DIR / "utrecht_stop_times.json"

WEEKDAY_FIELDS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

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
    """Filtert trips.txt op de gevonden U-OV buslijnen. Geeft naast de simpele
    trip->route-mapping (gebruikt door de realtime-fetchers) ook trip_meta
    terug (service_id + headsign, gebruikt door de haltezoeker/dienstregeling)."""
    trip_to_route = {}
    trip_meta = {}
    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            if row["route_id"] in route_ids:
                trip_to_route[row["trip_id"]] = row["route_id"]
                trip_meta[row["trip_id"]] = {
                    "route_id": row["route_id"],
                    "service_id": row.get("service_id", ""),
                    "headsign": row.get("trip_headsign", ""),
                }
    return trip_to_route, trip_meta


def find_calendar_for_services(zf, service_ids):
    """Parseert calendar.txt + calendar_dates.txt, beperkt tot de service_ids
    die daadwerkelijk door U-OV-trips gebruikt worden."""
    calendar = {}
    try:
        with zf.open("calendar.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                if row["service_id"] not in service_ids:
                    continue
                calendar[row["service_id"]] = {
                    "days": [row.get(day) == "1" for day in WEEKDAY_FIELDS],
                    "start_date": row.get("start_date", ""),
                    "end_date": row.get("end_date", ""),
                    "added": [],
                    "removed": [],
                }
    except KeyError:
        pass  # sommige GTFS-feeds laten calendar.txt weg en gebruiken alleen calendar_dates.txt

    try:
        with zf.open("calendar_dates.txt") as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            for row in reader:
                sid = row["service_id"]
                if sid not in service_ids:
                    continue
                entry = calendar.setdefault(
                    sid, {"days": [False] * 7, "start_date": "", "end_date": "", "added": [], "removed": []}
                )
                bucket = "added" if row.get("exception_type") == "1" else "removed"
                entry[bucket].append(row["date"])
    except KeyError:
        pass

    return calendar


def find_stop_times_for_trips(zf, trip_ids):
    """Streamt stop_times.txt (groot bestand, landelijk -- vaak tientallen
    miljoenen regels waarvan er maar een klein deel U-OV is) om te bepalen
    welke haltes deze trips aandoen (gebruikt door de alerts-fallback) en om
    de vertrektijden per halte te verzamelen (gebruikt door de haltezoeker/
    dienstregeling).

    Gebruikt bewust csv.reader (niet DictReader) met handmatige kolomindex, en
    slaat een match op als compacte tuple (niet als dict met vier keys): een
    dict per rij voor de volledige landelijke feed kost op een kleine VPS al
    gauw te veel geheugen/CPU-tijd."""
    stop_times_by_stop = {}
    with zf.open("stop_times.txt") as f:
        text = io.TextIOWrapper(f, encoding="utf-8-sig")
        reader = csv.reader(text)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        i_trip, i_stop, i_seq = idx["trip_id"], idx["stop_id"], idx["stop_sequence"]
        i_arr, i_dep = idx["arrival_time"], idx["departure_time"]
        for i, row in enumerate(reader):
            trip_id = row[i_trip]
            if trip_id in trip_ids:
                try:
                    stop_sequence = int(row[i_seq])
                except (ValueError, IndexError):
                    stop_sequence = 0
                time_str = row[i_dep] or row[i_arr]
                stop_times_by_stop.setdefault(row[i_stop], []).append((trip_id, stop_sequence, time_str))
            if i % 2_000_000 == 0 and i:
                log(f"  ...{i:,} stop_times regels verwerkt, {len(stop_times_by_stop):,} haltes gevonden")
    return set(stop_times_by_stop), stop_times_by_stop


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

        trip_to_route, trip_meta = find_trips_for_routes(zf, set(routes))
        log(f"{len(trip_to_route):,} trips gevonden voor deze lijnen.")

        service_ids = {m["service_id"] for m in trip_meta.values() if m["service_id"]}
        log(f"Parse calendar.txt/calendar_dates.txt voor {len(service_ids)} service_ids...")
        calendar = find_calendar_for_services(zf, service_ids)

        log("Bepaal haltes en halte-tijden die deze trips aandoen (doorzoekt een groot bestand, even geduld)...")
        stop_ids, stop_times_by_stop = find_stop_times_for_trips(zf, set(trip_to_route))
        stop_info = load_stop_info(zf, stop_ids)
        log(f"{len(stop_info):,} haltes gevonden, {sum(len(v) for v in stop_times_by_stop.values()):,} halte-tijden.")

    line_names = sorted({r["short_name"] for r in routes.values()}, key=lambda s: (len(s), s))
    log(f"Lijnnummers ({len(line_names)}): {', '.join(line_names)}")

    for r in routes.values():
        r["operator"] = classify_operator(r["short_name"], r["long_name"])
    operator_counts = {}
    unknown_lines = []
    for r in routes.values():
        operator_counts[r["operator"]] = operator_counts.get(r["operator"], 0) + 1
        if r["operator"] == UNKNOWN:
            unknown_lines.append(f"{r['short_name']} ({r['long_name']})")
    log(f"Verdeling Keolis/Transdev: {operator_counts}")
    if unknown_lines:
        log(
            "WAARSCHUWING: onherkende lijn(en), niet toegewezen aan Keolis of "
            "Transdev -- voeg toe aan app/concession_mapping.py: " + "; ".join(unknown_lines)
        )

    OUT_STOPS.write_text(json.dumps(stop_info, ensure_ascii=False), encoding="utf-8")
    OUT_ROUTES.write_text(json.dumps(routes, ensure_ascii=False), encoding="utf-8")
    OUT_TRIPS.write_text(json.dumps(trip_to_route, ensure_ascii=False), encoding="utf-8")
    OUT_CALENDAR.write_text(json.dumps(calendar, ensure_ascii=False), encoding="utf-8")
    OUT_TRIP_META.write_text(json.dumps(trip_meta, ensure_ascii=False), encoding="utf-8")
    OUT_STOP_TIMES.write_text(json.dumps(stop_times_by_stop, ensure_ascii=False), encoding="utf-8")
    log(
        f"Weggeschreven: {OUT_STOPS.name}, {OUT_ROUTES.name}, {OUT_TRIPS.name}, "
        f"{OUT_CALENDAR.name}, {OUT_TRIP_META.name}, {OUT_STOP_TIMES.name}"
    )


if __name__ == "__main__":
    main()
