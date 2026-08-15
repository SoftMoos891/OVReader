"""Tests voor het uitlezen van OVapi's eigen protobuf-extensies.

Die worden met een handgeschreven wire-format-parser gelezen (zie de
toelichting in app/gtfs_rt.py: het .proto compileren zou protoc als
build-dependency vereisen voor twee velden). Juist daarom hoort daar een
test bij: gaat er iets mis in die parser, dan levert 'ie stilzwijgend
None op en valt de app terug op de oude weg -- zonder enige foutmelding.

De testdata wordt hieronder met de hand ge-encodeerd, zodat de test
onafhankelijk is van een netwerkverbinding én van de protobuf-bibliotheek.
"""
from app.gtfs_rt import UtrechtIndex, _as_int32, parse_ovapi_extensions

# ── Minimale protobuf-encoder, alleen voor deze tests ─────────────────────

def _varint(value):
    """Protobuf base-128 varint. Negatieve int32 wordt (zoals protobuf zelf
    doet) als 64-bits twee-complement uitgeschreven."""
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field_number, wire_type):
    return _varint((field_number << 3) | wire_type)


def _bytes_field(field_number, payload):
    return _tag(field_number, 2) + _varint(len(payload)) + payload


def _varint_field(field_number, value):
    return _tag(field_number, 0) + _varint(value)


def _feed(entities):
    """FeedMessage met entity op veld 2."""
    return b"".join(_bytes_field(2, e) for e in entities)


def _vehicle_entity(entity_id, delay=None, realtime_trip_id=None):
    """FeedEntity{id=1, vehicle=4} met VehiclePosition{trip=1, ovapi=1003}."""
    trip = b""
    if realtime_trip_id is not None:
        trip = _bytes_field(1003, _bytes_field(1, realtime_trip_id.encode()))
    vehicle = _bytes_field(1, trip) if trip else b""
    if delay is not None:
        vehicle += _bytes_field(1003, _varint_field(1, delay))
    return _bytes_field(1, entity_id.encode()) + _bytes_field(4, vehicle)


def _trip_update_entity(entity_id, realtime_trip_id=None):
    """FeedEntity{id=1, trip_update=3} met TripUpdate{trip=1}."""
    trip = b""
    if realtime_trip_id is not None:
        trip = _bytes_field(1003, _bytes_field(1, realtime_trip_id.encode()))
    return _bytes_field(1, entity_id.encode()) + _bytes_field(3, _bytes_field(1, trip))


# ── _as_int32 ─────────────────────────────────────────────────────────────

def test_as_int32_leaves_positive_delay_untouched():
    assert _as_int32(587) == 587


def test_as_int32_converts_sign_extended_negative_delay():
    """Zonder deze correctie leest een bus die 308 seconden voorloopt als
    18446744069414584012 i.p.v. -308."""
    assert _as_int32((-308) + (1 << 64)) == -308


def test_as_int32_handles_boundaries():
    assert _as_int32(0) == 0
    assert _as_int32((1 << 31) - 1) == (1 << 31) - 1   # grootste int32
    assert _as_int32(1 << 31) == -(1 << 31)            # kleinste int32


# ── parse_ovapi_extensions ────────────────────────────────────────────────

def test_reads_positive_delay_from_vehicle_position():
    raw = _feed([_vehicle_entity("bus-1", delay=120)])

    assert parse_ovapi_extensions(raw)["bus-1"]["delay"] == 120


def test_reads_negative_delay_for_a_vehicle_running_early():
    raw = _feed([_vehicle_entity("bus-1", delay=-308)])

    assert parse_ovapi_extensions(raw)["bus-1"]["delay"] == -308


def test_reads_realtime_trip_id_from_vehicle_position():
    raw = _feed([_vehicle_entity("bus-1", realtime_trip_id="KEOLIS:5056:40001")])

    assert parse_ovapi_extensions(raw)["bus-1"]["realtime_trip_id"] == "KEOLIS:5056:40001"


def test_reads_realtime_trip_id_from_trip_update():
    """Trip-updates dragen de extensie op TripDescriptor, niet op een
    VehiclePosition -- die tak moet dus ook gevolgd worden."""
    raw = _feed([_trip_update_entity("rit-1", realtime_trip_id="QBUZZ:z400:6031")])

    result = parse_ovapi_extensions(raw)
    assert result["rit-1"]["realtime_trip_id"] == "QBUZZ:z400:6031"
    assert result["rit-1"]["delay"] is None


def test_reads_delay_and_realtime_trip_id_together():
    raw = _feed([_vehicle_entity("bus-1", delay=45, realtime_trip_id="KEOLIS:1:2")])

    assert parse_ovapi_extensions(raw) == {
        "bus-1": {"delay": 45, "realtime_trip_id": "KEOLIS:1:2"}
    }


def test_keeps_entities_apart_and_skips_those_without_extensions():
    raw = _feed([
        _vehicle_entity("bus-1", delay=10),
        _vehicle_entity("bus-2", delay=-20),
        _vehicle_entity("bus-3"),  # geen enkele extensie
    ])

    result = parse_ovapi_extensions(raw)
    assert result["bus-1"]["delay"] == 10
    assert result["bus-2"]["delay"] == -20
    assert "bus-3" not in result


def test_empty_or_truncated_feed_does_not_raise():
    """De parser leest ruwe bytes van een externe bron; die mag hooguit
    niets opleveren, nooit de collector omvergooien."""
    assert parse_ovapi_extensions(b"") == {}
    full = _feed([_vehicle_entity("bus-1", delay=120)])
    for cut in range(1, len(full)):
        parse_ovapi_extensions(full[:cut])  # mag niet crashen


def test_ignores_unknown_extension_shapes():
    """Een toekomstig extra veld binnen de extensie mag de rest niet
    verstoren."""
    ovapi = _varint_field(1, 60) + _bytes_field(99, b"iets nieuws")
    entity = _bytes_field(1, b"bus-1") + _bytes_field(4, _bytes_field(1003, ovapi))

    assert parse_ovapi_extensions(_feed([entity]))["bus-1"]["delay"] == 60


# ── UtrechtIndex.route_id_for -- terugval via realtime_trip_id ────────────

def _index(routes, trip_to_route, realtime_trips):
    """UtrechtIndex zonder reload() (die leest data/-bestanden van schijf)."""
    index = object.__new__(UtrechtIndex)
    index.routes = routes
    index.trip_to_route = trip_to_route
    index.realtime_trips = realtime_trips
    index.stops = {}
    index.trip_meta = {}
    return index


def test_route_id_comes_straight_from_the_feed_when_known():
    index = _index({"R1": {}}, {}, {})

    assert index.route_id_for("t1", "R1") == "R1"


def test_falls_back_to_trip_id_mapping():
    index = _index({"R1": {}}, {"t1": "R1"}, {})

    assert index.route_id_for("t1", None) == "R1"


def test_falls_back_to_realtime_trip_id_when_trip_id_is_unknown():
    """Het scenario waarvoor dit vangnet bestaat: na een hernummering van de
    dienstregeling stuurt de live feed trip_id's die nog niet in onze
    statische index staan."""
    index = _index({"R1": {}}, {}, {"KEOLIS:1:2": {"route_id": "R1", "headsign": "Utrecht CS"}})

    assert index.route_id_for("onbekend-trip", None, "KEOLIS:1:2") == "R1"


def test_returns_none_when_nothing_matches():
    index = _index({"R1": {}}, {}, {})

    assert index.route_id_for("t9", "R9", "ONBEKEND:9:9") is None


def test_ignores_realtime_trip_id_pointing_at_a_route_outside_the_index():
    """Een rit van een andere vervoerder mag niet alsnog binnenglippen."""
    index = _index({"R1": {}}, {}, {"ARR:9:9": {"route_id": "R9", "headsign": ""}})

    assert index.route_id_for(None, None, "ARR:9:9") is None


def test_realtime_trip_meta_returns_headsign():
    index = _index({}, {}, {"KEOLIS:1:2": {"route_id": "R1", "headsign": "Vleuten"}})

    assert index.realtime_trip_meta_for("KEOLIS:1:2")["headsign"] == "Vleuten"
    assert index.realtime_trip_meta_for(None) is None
    assert index.realtime_trip_meta_for("bestaat-niet") is None
