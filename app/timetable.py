"""Dienstregeling-opzoek: haltezoeker en eerstvolgende vertrekken, op basis
van de door build_static_index.py gegenereerde statische bestanden
(utrecht_stop_times.json, utrecht_calendar.json, utrecht_trip_meta.json),
verrijkt met live vertraging uit de realtime-databank (app/db.py)."""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

WEEKDAY_FIELDS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LIVE_DELAY_FRESHNESS_SECONDS = 20 * 60  # hoe oud een laatst-bekende vertraging nog mag zijn


class Timetable:
    def __init__(self):
        self.stops = {}          # stop_id -> {name, lat, lon}
        self.stop_times = {}     # stop_id -> [(trip_id, stop_sequence, time_str), ...]
        self.trip_meta = {}      # trip_id -> {route_id, service_id, headsign}
        self.calendar = {}       # service_id -> {days, start_date, end_date, added, removed}
        self.loaded_at = 0
        self.reload()

    def reload(self):
        stops_path = DATA_DIR / "utrecht_stops.json"
        stop_times_path = DATA_DIR / "utrecht_stop_times.json"
        trip_meta_path = DATA_DIR / "utrecht_trip_meta.json"
        calendar_path = DATA_DIR / "utrecht_calendar.json"

        # utrecht_stops.json bestaat al veel langer (gebruikt door UtrechtIndex)
        # -- laad 'm onafhankelijk zodat de haltezoeker op naam altijd werkt,
        # ook als de nieuwere dienstregelingbestanden hieronder nog ontbreken.
        self.stops = json.loads(stops_path.read_text(encoding="utf-8")) if stops_path.exists() else {}

        if not stop_times_path.exists():
            # Bestaande installaties hebben deze bestanden pas na een herbouw
            # van de statische index (build_static_index.py) -- eerstvolgende
            # vertrekken staan tot die tijd uit i.p.v. de hele app te laten crashen.
            print(
                "[timetable] utrecht_stop_times.json ontbreekt -- eerstvolgende "
                "vertrekken staan uit totdat app/build_static_index.py opnieuw is "
                "gedraaid (haltezoeken op naam werkt al wel)."
            )
            self.stop_times, self.trip_meta, self.calendar = {}, {}, {}
            self.loaded_at = time.time()
            return
        self.stop_times = json.loads(stop_times_path.read_text(encoding="utf-8"))
        self.trip_meta = json.loads(trip_meta_path.read_text(encoding="utf-8"))
        self.calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
        self.loaded_at = time.time()

    def search_stops(self, query, limit=25):
        """Zoekt haltes op (deel van de) naam, case-insensitive. Haltes waarvan
        de naam met de zoekterm begint staan bovenaan."""
        q = (query or "").strip().lower()
        if not q:
            return []
        starts, contains = [], []
        for stop_id, s in self.stops.items():
            name = s.get("name", "")
            name_lower = name.lower()
            if name_lower.startswith(q):
                starts.append((stop_id, s))
            elif q in name_lower:
                contains.append((stop_id, s))
        starts.sort(key=lambda x: x[1].get("name", ""))
        contains.sort(key=lambda x: x[1].get("name", ""))
        results = (starts + contains)[:limit]
        return [{"stop_id": sid, "name": s.get("name", ""), "lat": s.get("lat"), "lon": s.get("lon")}
                for sid, s in results]

    def active_service_ids(self, target_date: date):
        """Geeft de service_ids terug die op de opgegeven datum rijden,
        rekening houdend met calendar.txt (weekdag + geldigheidsperiode) en
        calendar_dates.txt-uitzonderingen (toegevoegd/geschrapt)."""
        date_str = target_date.strftime("%Y%m%d")
        weekday = target_date.weekday()  # maandag=0 .. zondag=6, komt overeen met WEEKDAY_FIELDS
        active = set()
        for service_id, entry in self.calendar.items():
            start, end = entry.get("start_date", ""), entry.get("end_date", "")
            in_range = bool(start) and bool(end) and start <= date_str <= end
            scheduled = in_range and bool(entry.get("days", [False] * 7)[weekday])
            if date_str in entry.get("removed", []):
                scheduled = False
            if date_str in entry.get("added", []):
                scheduled = True
            if scheduled:
                active.add(service_id)
        return active

    def _live_delay_by_trip(self, trip_ids, stop_id, now_ts):
        """Laatst bekende vertraging (seconden) per trip_id voor deze halte,
        binnen het freshness-venster."""
        if not trip_ids:
            return {}
        cutoff = now_ts - LIVE_DELAY_FRESHNESS_SECONDS
        placeholders = ",".join("?" * len(trip_ids))
        conn = db.get_conn()
        try:
            rows = conn.execute(
                f"""
                SELECT trip_id, arrival_delay, departure_delay
                FROM trip_delays
                WHERE stop_id = ? AND trip_id IN ({placeholders}) AND fetched_at >= ?
                GROUP BY trip_id
                HAVING fetched_at = MAX(fetched_at)
                """,
                [stop_id, *trip_ids, cutoff],
            ).fetchall()
        finally:
            conn.close()
        return {
            r["trip_id"]: r["arrival_delay"] if r["arrival_delay"] is not None else r["departure_delay"]
            for r in rows
        }

    def next_departures(self, stop_id, now_ts, window_minutes=90, limit=20):
        entries = self.stop_times.get(stop_id, [])
        if not entries:
            return []

        now_dt = datetime.fromtimestamp(now_ts)
        today = now_dt.date()
        candidates = []
        for day_offset in (-1, 0):
            d = today + timedelta(days=day_offset)
            active = self.active_service_ids(d)
            if not active:
                continue
            midnight = datetime.combine(d, datetime.min.time())
            for trip_id, _stop_sequence, time_str in entries:
                meta = self.trip_meta.get(trip_id)
                if not meta or meta.get("service_id") not in active:
                    continue
                seconds = _parse_gtfs_time(time_str)
                if seconds is None:
                    continue
                scheduled_dt = midnight + timedelta(seconds=seconds)
                candidates.append((scheduled_dt, trip_id, meta))

        window_start = now_dt - timedelta(minutes=1)  # kleine marge voor net-vertrokken bussen
        window_end = now_dt + timedelta(minutes=window_minutes)
        upcoming = [c for c in candidates if window_start <= c[0] <= window_end]
        upcoming.sort(key=lambda c: c[0])
        upcoming = upcoming[:limit]

        trip_ids = [c[1] for c in upcoming]
        delay_by_trip = self._live_delay_by_trip(trip_ids, stop_id, now_ts)

        results = []
        for scheduled_dt, trip_id, meta in upcoming:
            delay = delay_by_trip.get(trip_id)
            estimated_dt = scheduled_dt + timedelta(seconds=delay) if delay is not None else None
            results.append({
                "trip_id": trip_id,
                "route_id": meta.get("route_id"),
                "headsign": meta.get("headsign", ""),
                "scheduled_time": scheduled_dt.strftime("%H:%M"),
                "estimated_time": estimated_dt.strftime("%H:%M") if estimated_dt else None,
                "delay_seconds": delay,
                "is_live": delay is not None,
            })
        return results


def _parse_gtfs_time(time_str):
    """Parseert een GTFS-tijd ('HH:MM:SS', mag >=24:00:00 zijn voor diensten
    die na middernacht doorlopen) naar seconden sinds middernacht."""
    if not time_str:
        return None
    parts = time_str.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s
