"""Ophalen en filteren van GTFS-Realtime feeds (OVapi/NDOV) tot alleen data
die relevant is voor de provincie Utrecht."""
import json
import time
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

FEED_VEHICLE_POSITIONS = "https://gtfs.ovapi.nl/nl/vehiclePositions.pb"
FEED_TRIP_UPDATES = "https://gtfs.ovapi.nl/nl/tripUpdates.pb"
FEED_ALERTS = "https://gtfs.ovapi.nl/nl/alerts.pb"

REQUEST_TIMEOUT = 20


class UtrechtIndex:
    """In-memory index van welke route_id's/trip_id's tot de provincie Utrecht
    behoren, geladen uit de door build_static_index.py gegenereerde bestanden."""

    def __init__(self):
        self.routes = {}       # route_id -> {agency_id, agency_name, short_name, long_name}
        self.trip_to_route = {}  # trip_id -> route_id
        self.stops = {}        # stop_id -> {name, lat, lon}
        self.loaded_at = 0
        self.reload()

    def reload(self):
        routes_path = DATA_DIR / "utrecht_routes.json"
        trips_path = DATA_DIR / "utrecht_trips.json"
        stops_path = DATA_DIR / "utrecht_stops.json"
        if not routes_path.exists():
            raise RuntimeError(
                "utrecht_routes.json ontbreekt. Draai eerst app/build_static_index.py"
            )
        self.routes = json.loads(routes_path.read_text(encoding="utf-8"))
        self.trip_to_route = json.loads(trips_path.read_text(encoding="utf-8"))
        self.stops = json.loads(stops_path.read_text(encoding="utf-8"))
        self.loaded_at = time.time()

    def route_id_for(self, entity_trip, entity_route_id):
        """Bepaalt de relevante route_id voor een GTFS-RT entity, met fallback
        via de trip_id-mapping als route_id niet direct is meegegeven."""
        if entity_route_id and entity_route_id in self.routes:
            return entity_route_id
        if entity_trip and entity_trip in self.trip_to_route:
            return self.trip_to_route[entity_trip]
        return None

    def is_relevant_route(self, route_id):
        return route_id in self.routes


def _fetch_feed(url):
    feed = gtfs_realtime_pb2.FeedMessage()
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    feed.ParseFromString(resp.content)
    return feed


def fetch_vehicle_positions(index: UtrechtIndex):
    """Geeft lijst van dicts terug met voertuigposities binnen Utrecht."""
    feed = _fetch_feed(FEED_VEHICLE_POSITIONS)
    results = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle
        trip_id = vp.trip.trip_id if vp.HasField("trip") else None
        route_id = vp.trip.route_id if vp.HasField("trip") and vp.trip.route_id else None
        resolved_route = index.route_id_for(trip_id, route_id)
        if not resolved_route:
            continue
        if not vp.HasField("position"):
            continue
        results.append({
            "vehicle_id": vp.vehicle.id if vp.HasField("vehicle") else None,
            "trip_id": trip_id,
            "route_id": resolved_route,
            "lat": vp.position.latitude,
            "lon": vp.position.longitude,
            "speed": vp.position.speed if vp.position.HasField("speed") else None,
            "bearing": vp.position.bearing if vp.position.HasField("bearing") else None,
        })
    return results


def fetch_trip_updates_feed():
    """Haalt de trip-updates feed één keer op; wordt gedeeld door
    fetch_trip_delays en fetch_cancellations zodat we de feed niet dubbel
    bevragen (voorkomt onnodige load / rate-limiting bij de bron)."""
    return _fetch_feed(FEED_TRIP_UPDATES)


def parse_trip_delays(feed, index: UtrechtIndex):
    """Geeft lijst van dicts terug met vertragingen per halte-update binnen Utrecht."""
    results = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id if tu.HasField("trip") else None
        route_id = tu.trip.route_id if tu.HasField("trip") and tu.trip.route_id else None
        resolved_route = index.route_id_for(trip_id, route_id)
        if not resolved_route:
            continue
        for stu in tu.stop_time_update:
            arrival_delay = stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            departure_delay = stu.departure.delay if stu.HasField("departure") and stu.departure.HasField("delay") else None
            if arrival_delay is None and departure_delay is None:
                continue
            results.append({
                "trip_id": trip_id,
                "route_id": resolved_route,
                "stop_id": stu.stop_id,
                "stop_sequence": stu.stop_sequence,
                "arrival_delay": arrival_delay,
                "departure_delay": departure_delay,
            })
    return results


def parse_cancellations(feed, index: UtrechtIndex):
    """Geeft lijst van dicts terug met ritten die als vervallen (CANCELED)
    gemeld zijn in de trip-updates feed, binnen de provincie Utrecht."""
    results = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update.trip
        if trip.schedule_relationship != gtfs_realtime_pb2.TripDescriptor.CANCELED:
            continue
        trip_id = trip.trip_id or None
        route_id = trip.route_id or None
        resolved_route = index.route_id_for(trip_id, route_id)
        if not resolved_route:
            continue
        service_date = None
        if trip.start_date and len(trip.start_date) == 8:
            service_date = f"{trip.start_date[0:4]}-{trip.start_date[4:6]}-{trip.start_date[6:8]}"
        results.append({
            "trip_id": trip_id,
            "route_id": resolved_route,
            "service_date": service_date,  # None afgehandeld door caller (fallback op vandaag)
            "start_time": trip.start_time or None,
        })
    return results


_EFFECT_NAMES = {
    0: "NO_SERVICE", 1: "REDUCED_SERVICE", 2: "SIGNIFICANT_DELAYS",
    3: "DETOUR", 4: "ADDITIONAL_SERVICE", 5: "MODIFIED_SERVICE",
    6: "OTHER_EFFECT", 7: "UNKNOWN_EFFECT", 8: "STOP_MOVED",
    9: "NO_EFFECT", 10: "ACCESSIBILITY_ISSUE",
}


def fetch_alerts(index: UtrechtIndex):
    """Geeft lijst van dicts terug met actuele storingen/meldingen binnen Utrecht."""
    feed = _fetch_feed(FEED_ALERTS)
    results = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        route_ids = set()
        relevant = False
        for ie in alert.informed_entity:
            rid = index.route_id_for(
                ie.trip.trip_id if ie.HasField("trip") else None,
                ie.route_id if ie.route_id else None,
            )
            if rid:
                relevant = True
                route_ids.add(rid)
            elif ie.stop_id and ie.stop_id in index.stops:
                relevant = True
        if not relevant:
            continue

        def best_text(translated_string):
            if not translated_string.translation:
                return ""
            for t in translated_string.translation:
                if t.language in ("nl", "nl-NL"):
                    return t.text
            return translated_string.translation[0].text

        results.append({
            "alert_id": entity.id,
            "route_ids": sorted(route_ids),
            "header": best_text(alert.header_text),
            "description": best_text(alert.description_text),
            "effect": _EFFECT_NAMES.get(alert.effect, "UNKNOWN_EFFECT"),
        })
    return results
