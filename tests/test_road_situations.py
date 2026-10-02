"""Wegsituaties in de collector: de RSS-melding volgt een oorzaak die later binnenkomt.

Sinds 2 okt 2026 levert de DVZ RSS-bridge de geparste lijst (bridge_wegen.py); de
tests van de parser zelf (het lenen van de RWS-oorzaak) staan nu in de bridge
(rssbridge/tests/test_wegsituaties_ophalen.py). Hier krijgt de collector de lijst
zoals de bridge die geeft.
"""
from app import collector


def _situatie(**anders):
    s = {"situation_id": "NDW06_ongeval", "record_type": "Accident", "type_label": "Ongeval", "comment": None,
         "cause": None, "severity": "unknown", "start_time": "2026-10-01T04:43:32Z", "end_time": None,
         "lat": 52.0907, "lon": 5.1214, "road_number": None, "road_location": None}
    s.update(anders)
    return s


def test_rss_item_follows_a_cause_that_arrives_later(temp_db, monkeypatch):
    # Eerst het automatisch gedetecteerde ongeval zonder oorzaak, daarna met de
    # oorzaak van de RWS-verkeerscentrale (die de bridge dan al heeft geleend).
    rondes = iter([[_situatie()], [_situatie(cause="Defecte vrachtwagen", type_label="Defecte vrachtwagen")]])
    monkeypatch.setattr(collector, "fetch_utrecht_road_situations", lambda: next(rondes))

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
