"""Ophalen en filteren van GTFS-Realtime feeds (OVapi/NDOV) tot alleen data
die relevant is voor de provincie Utrecht."""
import json
import time
from pathlib import Path

import requests
from google.transit import gtfs_realtime_pb2

from .concession_mapping import TRANSDEV_TRAM

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

FEED_VEHICLE_POSITIONS = "https://gtfs.ovapi.nl/nl/vehiclePositions.pb"
FEED_TRIP_UPDATES = "https://gtfs.ovapi.nl/nl/tripUpdates.pb"
FEED_ALERTS = "https://gtfs.ovapi.nl/nl/alerts.pb"

REQUEST_TIMEOUT = 20

# ── OVapi-eigen protobuf-extensies (veldnummer 1003) ──────────────────────
# OVapi publiceert naast de standaard GTFS-Realtime-definitie een eigen
# uitbreiding (https://gtfs.ovapi.nl/nl/gtfs-realtime-OVapi.proto) met velden
# die de standaard niet kent. De google.transit-bindings weten daar niets
# van, dus die gooien ze bij het parsen stilzwijgend weg.
#
# We lezen ze daarom met een minimale eigen wire-format-parser uit de ruwe
# bytes, i.p.v. het .proto-bestand te compileren: dat zou protoc/grpcio-tools
# als extra build-dependency vereisen plus een gegenereerd bestand dat bij
# elke deploy mee moet -- veel omslachtiger dan de ~25 regels hieronder voor
# de twee velden die we daadwerkelijk nodig hebben. De standaardparser blijft
# gewoon al het echte werk doen; dit is puur een tweede, oppervlakkige pass
# over dezelfde bytes om er de extensies bij te zoeken.
#
# Geverifieerd tegen de live feed: delay is in ~96% van de voertuigposities
# gevuld, realtime_trip_id in ~78% van de trip-updates.
_OVAPI_EXT_FIELD = 1003


def _read_varint(buf, i):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def _parse_wire_fields(buf):
    """Ontleedt een protobuf-bericht tot {veldnummer: [ruwe waarde, ...]}.
    Kent geen schema: length-delimited velden (wire type 2) komen als bytes
    terug, varints als int. Genoeg om gericht in een boodschap te 'graven'.

    Bij afgekapte of anderszins onzinnige bytes stopt het ontleden gewoon en
    komt terug wat er tot dan toe wél uit kwam -- dit leest data van een
    externe bron, en die mag hooguit niets opleveren, nooit de collector
    omvergooien."""
    out = {}
    i = 0
    while i < len(buf):
        try:
            key, i = _read_varint(buf, i)
            field_number, wire_type = key >> 3, key & 7
            if wire_type == 0:
                value, i = _read_varint(buf, i)
            elif wire_type == 2:
                length, i = _read_varint(buf, i)
                if i + length > len(buf):
                    break  # afgekapt: de rest is niet te vertrouwen
                value, i = buf[i:i + length], i + length
            elif wire_type == 5:
                value, i = buf[i:i + 4], i + 4
            elif wire_type == 1:
                value, i = buf[i:i + 8], i + 8
            else:
                break  # onbekend wire type: rest van deze boodschap overslaan
        except IndexError:
            break  # varint liep voorbij het einde van de buffer
        out.setdefault(field_number, []).append(value)
    return out


def _as_int32(value):
    """Protobuf codeert een negatieve int32 als een tot 64 bits
    teken-uitgebreide varint -- zonder deze correctie leest een vertraging
    van -308 seconden (bus rijdt voor) als 18446744069414584012."""
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value


def parse_ovapi_extensions(raw):
    """Haalt de OVapi-extensievelden uit een ruwe GTFS-RT feed.

    Geeft {entity_id: {"delay": int|None, "realtime_trip_id": str|None}}.
    entity_id is dezelfde sleutel als entity.id in de normaal geparste feed,
    zodat beide passes op elkaar te leggen zijn.

    Veldnummers volgen de GTFS-Realtime-spec: FeedMessage.entity=2,
    FeedEntity.trip_update=3 / .vehicle=4, VehiclePosition.trip=1,
    TripUpdate.trip=1. Binnen de extensies (zie het .proto hierboven):
    OVapiVehiclePosition.delay=1, OVapiTripDescriptor.realtime_trip_id=1."""
    result = {}
    for entity_buf in _parse_wire_fields(raw).get(2, []):
        entity = _parse_wire_fields(entity_buf)
        if 1 not in entity:
            continue
        entity_id = entity[1][0].decode("utf-8", "replace")
        info = {"delay": None, "realtime_trip_id": None}

        # vehicle (4) of trip_update (3) -- allebei hebben ze de
        # TripDescriptor op veld 1, waar realtime_trip_id in zit.
        body_buf = (entity.get(4) or entity.get(3) or [None])[0]
        if body_buf is None:
            continue
        body = _parse_wire_fields(body_buf)

        if _OVAPI_EXT_FIELD in body:  # alleen VehiclePosition heeft delay
            ext = _parse_wire_fields(body[_OVAPI_EXT_FIELD][0])
            if 1 in ext:
                info["delay"] = _as_int32(ext[1][0])
        if 1 in body:
            trip = _parse_wire_fields(body[1][0])
            if _OVAPI_EXT_FIELD in trip:
                ext = _parse_wire_fields(trip[_OVAPI_EXT_FIELD][0])
                if 1 in ext:
                    info["realtime_trip_id"] = ext[1][0].decode("utf-8", "replace")
        if info["delay"] is not None or info["realtime_trip_id"]:
            result[entity_id] = info
    return result


class UtrechtIndex:
    """In-memory index van welke route_id's/trip_id's tot de provincie Utrecht
    behoren, geladen uit de door build_static_index.py gegenereerde bestanden."""

    def __init__(self):
        self.routes = {}       # route_id -> {agency_id, agency_name, short_name, long_name}
        self.trip_to_route = {}  # trip_id -> route_id
        self.stops = {}        # stop_id -> {name, lat, lon}
        self.trip_meta = {}    # trip_id -> {route_id, service_id, headsign}
        # realtime_trip_id ("KEOLIS:5056:40001") -> {route_id, headsign}; zie
        # realtime_trip_meta_for() hieronder voor waarom dit bestaat.
        self.realtime_trips = {}
        self.loaded_at = 0
        self.reload()

    def reload(self):
        routes_path = DATA_DIR / "utrecht_routes.json"
        trips_path = DATA_DIR / "utrecht_trips.json"
        stops_path = DATA_DIR / "utrecht_stops.json"
        trip_meta_path = DATA_DIR / "utrecht_trip_meta.json"
        realtime_trips_path = DATA_DIR / "utrecht_realtime_trips.json"
        if not routes_path.exists():
            raise RuntimeError(
                "utrecht_routes.json ontbreekt. Draai eerst app/build_static_index.py"
            )
        self.routes = json.loads(routes_path.read_text(encoding="utf-8"))
        self.trip_to_route = json.loads(trips_path.read_text(encoding="utf-8"))
        self.stops = json.loads(stops_path.read_text(encoding="utf-8"))
        self.trip_meta = (
            json.loads(trip_meta_path.read_text(encoding="utf-8")) if trip_meta_path.exists() else {}
        )
        # Ontbreekt op installaties waar de statische index nog niet opnieuw is
        # gebouwd sinds dit bestand werd toegevoegd -- dan valt alles gewoon
        # terug op het oude gedrag (matchen op trip_id).
        self.realtime_trips = (
            json.loads(realtime_trips_path.read_text(encoding="utf-8"))
            if realtime_trips_path.exists() else {}
        )
        self.loaded_at = time.time()

    def realtime_trip_meta_for(self, realtime_trip_id):
        """{route_id, headsign} voor een OVapi realtime_trip_id, of None.

        Dit is het vangnet tegen het terugkerende probleem dat vervoerders bij
        een dienstregelingwijziging alle trip_id's hernummeren: de live feed
        stuurt dan trip_id's die nog niet in onze statische index staan, en
        alles valt stil tot die opnieuw is gebouwd (is in dit project al twee
        keer gebeurd). realtime_trip_id is semantisch
        (vervoerder:lijnplanning:ritnummer) en blijft over zo'n hernummering
        heen wél gelijk.

        Het is geen vervanging van trip_id: meerdere trip_id's (verschillende
        dienstdagen) delen dezelfde realtime_trip_id. Voor route en headsign
        maakt dat niet uit -- geverifieerd tegen de volledige feed: van de
        49.432 unieke realtime_trip_id's wijst er geen enkele naar meer dan
        één route_id of headsign."""
        return self.realtime_trips.get(realtime_trip_id) if realtime_trip_id else None

    def route_id_for(self, entity_trip, entity_route_id, realtime_trip_id=None):
        """Bepaalt de relevante route_id voor een GTFS-RT entity, met fallback
        via de trip_id-mapping als route_id niet direct is meegegeven, en als
        laatste redmiddel via realtime_trip_id (zie
        realtime_trip_meta_for())."""
        if entity_route_id and entity_route_id in self.routes:
            return entity_route_id
        if entity_trip and entity_trip in self.trip_to_route:
            return self.trip_to_route[entity_trip]
        meta = self.realtime_trip_meta_for(realtime_trip_id)
        if meta and meta.get("route_id") in self.routes:
            return meta["route_id"]
        return None

    def is_relevant_route(self, route_id):
        return route_id in self.routes

    def is_bus_route(self, route_id):
        """True als de route tot de huidige index behoort én geen tram is.
        De U-tram (Transdev tram) blijft zichtbaar op de kaart, maar de
        realtime trip-updates feed levert er geen bruikbare vertragingen/
        uitval voor -- daarom wordt deze check gebruikt om trams buiten de
        vertragingen- en uitvalstatistieken te houden (zie server.py/
        records.py), zonder ze uit de rest van de app te filteren."""
        route = self.routes.get(route_id)
        if route is None:
            return False
        return route.get("operator") != TRANSDEV_TRAM


def _fetch_feed(url):
    """Geeft (feed, ruwe bytes) terug. De ruwe bytes zijn nodig voor de
    tweede pass die de OVapi-extensies eruit haalt (zie
    parse_ovapi_extensions) -- de standaardparser bewaart die niet."""
    feed = gtfs_realtime_pb2.FeedMessage()
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    feed.ParseFromString(resp.content)
    return feed, resp.content


_CURRENT_STATUS_NAMES = {0: "INCOMING_AT", 1: "STOPPED_AT", 2: "IN_TRANSIT_TO"}


def fetch_vehicle_positions(index: UtrechtIndex):
    """Geeft lijst van dicts terug met voertuigposities binnen Utrecht."""
    feed, raw = _fetch_feed(FEED_VEHICLE_POSITIONS)
    extensions = parse_ovapi_extensions(raw)
    results = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vp = entity.vehicle
        ext = extensions.get(entity.id) or {}
        realtime_trip_id = ext.get("realtime_trip_id")
        trip_id = vp.trip.trip_id if vp.HasField("trip") else None
        route_id = vp.trip.route_id if vp.HasField("trip") and vp.trip.route_id else None
        resolved_route = index.route_id_for(trip_id, route_id, realtime_trip_id)
        if not resolved_route:
            continue
        if not vp.HasField("position"):
            continue
        results.append({
            # vehicle.id staat in de praktijk nooit gevuld (OVapi) -- het
            # echte voertuignummer (bv. "7054") zit in vehicle.label.
            "vehicle_id": vp.vehicle.label if vp.HasField("vehicle") else None,
            "trip_id": trip_id,
            "route_id": resolved_route,
            "direction_id": vp.trip.direction_id if vp.HasField("trip") and vp.trip.HasField("direction_id") else None,
            "current_status": _CURRENT_STATUS_NAMES.get(vp.current_status, "IN_TRANSIT_TO"),
            # De halte waar current_status betrekking op heeft: bij STOPPED_AT
            # de halte waar het voertuig nu stilstaat, anders de eerstvolgende
            # halte (INCOMING_AT/IN_TRANSIT_TO).
            "stop_id": vp.stop_id or None,
            "lat": vp.position.latitude,
            "lon": vp.position.longitude,
            "speed": vp.position.speed if vp.position.HasField("speed") else None,
            "bearing": vp.position.bearing if vp.position.HasField("bearing") else None,
            # Opgeslagen op het moment zelf (i.p.v. achteraf via trip_id
            # opgezocht) zodat /api/vehicles/history de bestemming nog klopt
            # nadat de statische index inmiddels is herbouwd en dit trip_id
            # er niet meer in voorkomt. Valt terug op de realtime_trip_id-
            # mapping als dit trip_id (nog) niet in de index staat.
            "headsign": (
                index.trip_meta.get(trip_id, {}).get("headsign")
                or (index.realtime_trip_meta_for(realtime_trip_id) or {}).get("headsign")
                or None
            ),
            # Vertraging in seconden, rechtstreeks uit OVapi's eigen
            # extensie op de voertuigfeed (~96% gevuld). Scheelt een aparte
            # koppeling met trip_delays, die voor sommige ritten helemaal
            # geen rij heeft -- die verschenen dan als "Onbekend" op de kaart
            # terwijl de positie wel binnenkwam.
            "delay_seconds": ext.get("delay"),
        })
    return results


def fetch_trip_updates_feed():
    """Haalt de trip-updates feed één keer op; wordt gedeeld door
    parse_trip_delays en parse_cancellations zodat we de feed niet dubbel
    bevragen (voorkomt onnodige load / rate-limiting bij de bron). Geeft
    (feed, ovapi-extensies) terug -- de extensies worden hier één keer uit
    de ruwe bytes gehaald i.p.v. in elke parser opnieuw."""
    feed, raw = _fetch_feed(FEED_TRIP_UPDATES)
    return feed, parse_ovapi_extensions(raw)


def parse_trip_delays(feed, index: UtrechtIndex, extensions=None):
    """Geeft lijst van dicts terug met vertragingen per halte-update binnen Utrecht."""
    extensions = extensions or {}
    results = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id if tu.HasField("trip") else None
        route_id = tu.trip.route_id if tu.HasField("trip") and tu.trip.route_id else None
        resolved_route = index.route_id_for(
            trip_id, route_id, extensions.get(entity.id, {}).get("realtime_trip_id")
        )
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


def parse_cancellations(feed, index: UtrechtIndex, extensions=None):
    """Geeft lijst van dicts terug met ritten die als vervallen (CANCELED)
    gemeld zijn in de trip-updates feed, binnen de provincie Utrecht."""
    extensions = extensions or {}
    results = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update.trip
        if trip.schedule_relationship != gtfs_realtime_pb2.TripDescriptor.CANCELED:
            continue
        trip_id = trip.trip_id or None
        route_id = trip.route_id or None
        resolved_route = index.route_id_for(
            trip_id, route_id, extensions.get(entity.id, {}).get("realtime_trip_id")
        )
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


# parse_skipped_stops() stond hier: individuele tussenhaltes die de feed als
# SKIPPED meldt (rit rijdt door zonder te stoppen). De enige weergave ervan
# ("Vaak overgeslagen haltes" op /uitval) is verwijderd, waarna de collector
# die tabel alleen nog maar zat te vullen -- op verzoek gestopt, en daarmee
# is deze parser vervallen. De feed levert de gegevens nog steeds; zie de
# git-geschiedenis van dit bestand als het ooit terug moet.


_EFFECT_NAMES = {
    0: "NO_SERVICE", 1: "REDUCED_SERVICE", 2: "SIGNIFICANT_DELAYS",
    3: "DETOUR", 4: "ADDITIONAL_SERVICE", 5: "MODIFIED_SERVICE",
    6: "OTHER_EFFECT", 7: "UNKNOWN_EFFECT", 8: "STOP_MOVED",
    9: "NO_EFFECT", 10: "ACCESSIBILITY_ISSUE",
}

# GTFS-RT Alert.Cause -- los van effect (dat zegt iets over het GEVOLG voor
# de dienst, bv. "Omleiding"); cause zegt iets over de OORZAAK. Vervoerders
# vullen dit veld lang niet altijd betrouwbaar (in de praktijk is UNKNOWN_CAUSE
# verreweg het vaakst voorkomend), maar als het wel gezet is (bv.
# POLICE_ACTIVITY) is dat een sterker signaal dan tekst-keywords zoeken.
_CAUSE_NAMES = {
    1: "UNKNOWN_CAUSE", 2: "OTHER_CAUSE", 3: "TECHNICAL_PROBLEM",
    4: "STRIKE", 5: "DEMONSTRATION", 6: "ACCIDENT", 7: "HOLIDAY",
    8: "WEATHER", 9: "MAINTENANCE", 10: "CONSTRUCTION",
    11: "POLICE_ACTIVITY", 12: "MEDICAL_EMERGENCY",
}


def fetch_alerts(index: UtrechtIndex):
    """Geeft lijst van dicts terug met actuele storingen/meldingen binnen Utrecht."""
    feed, _raw = _fetch_feed(FEED_ALERTS)
    results = []
    for entity in feed.entity:
        if not entity.HasField("alert"):
            continue
        alert = entity.alert
        route_ids = set()
        stop_ids = set()
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
                stop_ids.add(ie.stop_id)
        if not relevant:
            continue

        def best_text(translated_string):
            if not translated_string.translation:
                return ""
            for t in translated_string.translation:
                if t.language in ("nl", "nl-NL"):
                    return t.text
            return translated_string.translation[0].text

        # De feed geeft in de praktijk precies één active_period per alert
        # (zie onderzoek in ns_rail_alerts.py-achtige verkenning); bij meer
        # dan één nemen we toch de ruimst mogelijke periode.
        valid_from = min((p.start for p in alert.active_period if p.HasField("start")), default=None)
        valid_until = max((p.end for p in alert.active_period if p.HasField("end")), default=None)

        results.append({
            "alert_id": entity.id,
            "route_ids": sorted(route_ids),
            # Bij een melding die alleen een halte noemt (geen route/trip in
            # informed_entity, bv. "halte vervalt i.v.m. brandweer") is
            # route_ids leeg -- zonder dit veld is dan nergens te zien om
            # welke halte het gaat.
            "stop_ids": sorted(stop_ids),
            "header": best_text(alert.header_text),
            "description": best_text(alert.description_text),
            "effect": _EFFECT_NAMES.get(alert.effect, "UNKNOWN_EFFECT"),
            "cause": _CAUSE_NAMES.get(alert.cause, "UNKNOWN_CAUSE"),
            "valid_from": valid_from,
            "valid_until": valid_until,
        })
    return results
