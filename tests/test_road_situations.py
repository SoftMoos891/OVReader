"""Oorzaak van de RWS-verkeerscentrale bij een wegsituatie (1 okt 2026): een
automatisch gedetecteerd ongeval zonder oorzaak leent die van een situatie vlak
ernaast, en de oorzaak gaat dan voor op het type-label."""
from xml.etree import ElementTree as ET

from app import collector, road_situations as rs

# Midden in de stad Utrecht, zodat de provinciecheck slaagt. 0.00165 graad
# lengte is hier ~113 m, ongeveer de afstand uit het A12-voorbeeld.
LAT, LON = 52.0907, 5.1214

_HEAD = (
    '<mc:messageContainer xmlns:mc="http://datex2.eu/schema/3/messageContainer" '
    'xmlns:sit="http://datex2.eu/schema/3/situation" xmlns:com="http://datex2.eu/schema/3/common" '
    'xmlns:loc="http://datex2.eu/schema/3/locationReferencing" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><mc:payload>'
)


def _record(rtype, rid, start, *, cause=None, bearing=None, point=None, line=None):
    cause_xml = (
        f"<sit:cause><sit:causeDescription><com:values><com:value lang=\"nl\">{cause}</com:value>"
        "</com:values></sit:causeDescription><sit:causeType>other</sit:causeType></sit:cause>"
        if cause else ""
    )
    if line:
        loc = ("<sit:locationReference><loc:gmlLineString><loc:posList>"
               + " ".join(f"{la} {lo}" for la, lo in line) + "</loc:posList></loc:gmlLineString></sit:locationReference>")
    else:
        la, lo = point
        loc = ("<sit:locationReference><loc:pointByCoordinates>"
               + (f"<loc:bearing>{bearing}</loc:bearing>" if bearing is not None else "")
               + f"<loc:pointCoordinates><loc:latitude>{la}</loc:latitude><loc:longitude>{lo}</loc:longitude>"
               "</loc:pointCoordinates></loc:pointByCoordinates></sit:locationReference>")
    return (
        f'<sit:situationRecord xsi:type="sit:{rtype}" id="{rid}_1">'
        f"<sit:validity><com:validityTimeSpecification><com:overallStartTime>{start}</com:overallStartTime>"
        f"</com:validityTimeSpecification></sit:validity>{cause_xml}{loc}</sit:situationRecord>"
    )


def _feed(*situations):
    body = "".join(f'<sit:situation id="{sid}"><sit:overallSeverity>unknown</sit:overallSeverity>{rec}</sit:situation>'
                   for sid, rec in situations)
    return ET.fromstring(_HEAD + body + "</mc:payload></mc:messageContainer>")


def _accident(bearing=270, start="2026-10-01T04:43:32Z"):
    return ("NDW06_ongeval", _record("Accident", "NDW06_ongeval", start, bearing=bearing, point=(LAT, LON)))


def _rws(cause="Defecte vrachtwagen", bearing=270, dlon=0.00165, start="2026-10-01T04:51:32Z", rtype="RoadOrCarriagewayOrLaneManagement"):
    return ("NLRWS_1", _record(rtype, "NLRWS_1", start, cause=cause, bearing=bearing, point=(LAT, LON + dlon)))


def _by_id(results):
    return {r["situation_id"]: r for r in results}


def test_accident_borrows_rws_cause_and_takes_it_as_label():
    r = _by_id(rs.parse_road_situations(_feed(_accident(), _rws())))
    assert r["NDW06_ongeval"]["cause"] == "Defecte vrachtwagen"
    assert r["NDW06_ongeval"]["type_label"] == "Defecte vrachtwagen"
    assert r["NDW06_ongeval"]["record_type"] == "Accident"   # urgentie en RSS-keuze blijven gelijk
    # De RWS-afsluiting zelf heette eerst "Wegwerkzaamheden"; nu de eigen oorzaak.
    assert r["NLRWS_1"]["type_label"] == "Defecte vrachtwagen"


def test_rws_spelling_is_cleaned_up():
    r = _by_id(rs.parse_road_situations(_feed(_accident(), _rws(cause="Ongeval(len)"))))
    assert r["NDW06_ongeval"]["type_label"] == "Ongeval"
    assert rs.clean_cause("Ongeval(len)") == "Ongeval"


def test_no_borrow_from_the_other_direction():
    r = _by_id(rs.parse_road_situations(_feed(_accident(bearing=270), _rws(bearing=90))))
    assert r["NDW06_ongeval"]["cause"] is None
    assert r["NDW06_ongeval"]["type_label"] == "Ongeval"


def test_no_borrow_beyond_radius():
    # 0.005 graad lengte is ~340 m
    r = _by_id(rs.parse_road_situations(_feed(_accident(), _rws(dlon=0.005))))
    assert r["NDW06_ongeval"]["cause"] is None


def test_no_borrow_when_started_hours_apart():
    r = _by_id(rs.parse_road_situations(_feed(_accident(), _rws(start="2026-10-01T01:00:00Z"))))
    assert r["NDW06_ongeval"]["cause"] is None


def test_roadworks_cause_is_not_lent_to_an_accident():
    r = _by_id(rs.parse_road_situations(_feed(_accident(), _rws(cause="Wegwerkzaamheden"))))
    assert r["NDW06_ongeval"]["cause"] is None
    assert r["NDW06_ongeval"]["type_label"] == "Ongeval"


def test_traffic_jam_keeps_its_label_and_lends_nothing():
    # File van 2 km met oorzaak; een voertuig aan de staart (vaak een pijlwagen)
    # mag die oorzaak niet overnemen.
    file_line = [(LAT, LON + 0.03), (LAT, LON)]
    jam = ("NLRWS_file", _record("AbnormalTraffic", "NLRWS_file", "2026-10-01T04:50:00Z",
                                 cause="Defecte vrachtwagen", line=file_line))
    vehicle = ("NDW06_voertuig", _record("VehicleObstruction", "NDW06_voertuig", "2026-10-01T05:00:00Z",
                                         bearing=270, point=(LAT, LON + 0.0005)))
    r = _by_id(rs.parse_road_situations(_feed(jam, vehicle)))
    assert r["NLRWS_file"]["cause"] == "Defecte vrachtwagen"
    assert r["NLRWS_file"]["type_label"] == "Abnormale verkeersdrukte"
    assert r["NDW06_voertuig"]["cause"] is None


def test_rss_item_follows_a_cause_that_arrives_later(temp_db, monkeypatch):
    feeds = iter([_feed(_accident()), _feed(_accident(), _rws())])
    monkeypatch.setattr(collector, "fetch_utrecht_road_situations",
                        lambda: rs.parse_road_situations(next(feeds)))

    collector.fetch_road_situations_job()
    conn = temp_db.get_conn()
    first = conn.execute("SELECT title, pub_date FROM rss_feed_items WHERE guid='road-situation-NDW06_ongeval'").fetchone()
    assert first["title"].startswith("Wegsituatie (Ongeval)")

    collector.fetch_road_situations_job()
    second = conn.execute("SELECT title, description, pub_date FROM rss_feed_items "
                          "WHERE guid='road-situation-NDW06_ongeval'").fetchone()
    assert second["title"].startswith("Wegsituatie (Defecte vrachtwagen)")
    assert second["description"].startswith("Defecte vrachtwagen")
    assert second["pub_date"] == first["pub_date"]
