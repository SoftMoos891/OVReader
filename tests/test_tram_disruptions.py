"""Tests voor de tramanalyse op /trends -- zie app/tram_disruptions.py."""
import sqlite3
import time
from datetime import date, datetime, time as dtime, timedelta

import pytest

from app import tram_disruptions as td


class FakeIndex:
    """Minimale UtrechtIndex-vervanger: alleen wat is_tram_alert() aanraakt."""

    def __init__(self, tram_stop_ids=(), tram_routes=None):
        self.tram_stop_ids = set(tram_stop_ids)
        self.routes = tram_routes or {"T20": {"short_name": "20"}, "T22": {"short_name": "22"}}
        self.routes.setdefault("B31", {"short_name": "31"})

    def tram_route_ids(self):
        return {rid for rid, r in self.routes.items() if r["short_name"] in {"20", "21", "22"}}


def alert(**overrides):
    row = {
        "alert_id": "a1",
        "route_ids": "",
        "stop_ids": "",
        "header": "",
        "description": "",
        "cause": "OTHER_CAUSE",
        "effect": "UNKNOWN_EFFECT",
        "first_seen": 1_700_000_000,
        "last_seen": 1_700_003_600,
        "valid_from": None,
        "valid_until": None,
    }
    row.update(overrides)
    return row


def epoch_at(day, hour=12):
    return int(datetime.combine(day, dtime(hour, 0)).timestamp())


# ── herkenning ───────────────────────────────────────────────────────────

def test_tramhalte_maakt_melding_een_trammelding():
    """Het belangrijkste geval: de tekst verraadt niets, de haltelijst wel."""
    index = FakeIndex(tram_stop_ids={"3924077"})
    row = alert(stop_ids="3924077,9999",
                header="U-OV: vanwege een storing is de dienstregeling tijdelijk verstoord.")
    assert td.is_tram_alert(row, index)


def test_tramroute_maakt_melding_een_trammelding():
    row = alert(route_ids="T20")
    assert td.is_tram_alert(row, FakeIndex())


def test_tekst_is_vangnet_zonder_haltes():
    row = alert(header="U-OV: Wegens een tramstoring rijden de trams niet.")
    assert td.is_tram_alert(row, FakeIndex())


def test_busmelding_die_de_tram_alleen_als_overstap_noemt_telt_niet():
    """'Lijn 31: ... U kunt overstappen op de tram' is een busmelding."""
    row = alert(header="Lijn 31: bus rijdt niet verder dan Nieuwegein Centrum.",
                description="U kunt vanaf Nieuwegein Centrum overstappen op de tram.")
    assert not td.is_tram_alert(row, FakeIndex())


def test_melding_met_tramlijnnummer_voorop_telt_wel():
    row = alert(header="Lijn 22: geen tramverkeer meer mogelijk tussen Science Park en Utrecht CS.")
    assert td.is_tram_alert(row, FakeIndex())


def test_busroute_sluit_tekstherkenning_uit():
    row = alert(route_ids="B31", header="Bus rijdt om, gebruik de tram.")
    assert not td.is_tram_alert(row, FakeIndex())


def test_melding_zonder_tram_telt_niet():
    row = alert(header="U-OV: halte vervalt i.v.m. werkzaamheden.")
    assert not td.is_tram_alert(row, FakeIndex(tram_stop_ids={"3924077"}))


# ── oorzaak en geplande situaties ────────────────────────────────────────

def test_gemelde_oorzaak_gaat_voor_de_gtfs_oorzaak():
    row = alert(description="Oorzaak : Stroomstoring \nEffect : Omleiding \n", cause="OTHER_CAUSE")
    assert td.reported_cause(row) == "Stroomstoring"
    assert td.reported_effect(row) == "Omleiding"


def test_zonder_gemelde_oorzaak_valt_hij_terug_op_gtfs():
    assert td.reported_cause(alert(cause="ACCIDENT")) == "Ongeval"
    assert td.reported_cause(alert(cause=None)) == "Niet gemeld"


def test_werkzaamheden_zijn_gepland():
    assert td.is_planned(alert(description="Oorzaak : Werkzaamheden \n"))
    assert td.is_planned(alert(cause="MAINTENANCE"))


def test_aankondiging_ver_vooruit_is_gepland():
    row = alert(first_seen=1_700_000_000, valid_from=1_700_000_000 + 3 * 3600)
    assert td.is_planned(row)


def test_meerdaagse_geldigheidsperiode_is_gepland():
    """'Halte is buiten gebruik van 7 t/m 11 september', oorzaak 'Stremming':
    geen geplande oorzaak, niet ver vooruit gemeld, maar wel meerdaags."""
    row = alert(description="Oorzaak : Stremming \n",
                first_seen=1_700_000_000,
                valid_from=1_700_000_500,
                valid_until=1_700_000_500 + 3 * 86400)
    assert td.is_planned(row)


def test_acute_storing_is_niet_gepland():
    row = alert(description="Oorzaak : Stremming \n", cause="OTHER_CAUSE",
                first_seen=1_700_000_000, valid_from=1_700_000_060,
                valid_until=1_700_000_060 + 3600)
    assert not td.is_planned(row)


# ── samenvoegen tot storingsperiodes ─────────────────────────────────────

def test_opeenvolgende_meldingen_worden_een_storing():
    alerts = [
        {"first_seen": 1000, "last_seen": 2000, "cause_label": "Stremming", "header": "eerste"},
        {"first_seen": 2500, "last_seen": 5000, "cause_label": "Eerdere verstoring", "header": "tweede"},
    ]
    incidents = td.group_into_incidents(alerts)
    assert len(incidents) == 1
    assert incidents[0]["duration_seconds"] == 4000
    # De oorzaak komt van de eerste melding: vervolgmeldingen beschrijven het
    # gevolg, niet wat er gebeurd is.
    assert incidents[0]["cause"] == "Stremming"


def test_meldingen_ver_uit_elkaar_zijn_losse_storingen():
    gap = td.INCIDENT_MERGE_GAP_SECONDS + 60
    alerts = [
        {"first_seen": 1000, "last_seen": 2000, "cause_label": "Ongeval", "header": "a"},
        {"first_seen": 2000 + gap, "last_seen": 3000 + gap, "cause_label": "Stremming", "header": "b"},
    ]
    assert len(td.group_into_incidents(alerts)) == 2


# ── overzicht ────────────────────────────────────────────────────────────

def test_build_overview_telt_storingen_en_laat_lege_dagen_staan():
    today = date.today()
    since = today - timedelta(days=2)
    rows = [
        alert(alert_id="a", first_seen=epoch_at(since, 8), last_seen=epoch_at(since, 10),
              description="Oorzaak : Ongeval \nEffect : Omleiding \n"),
        alert(alert_id="b", first_seen=epoch_at(today, 9), last_seen=epoch_at(today, 10),
              description="Oorzaak : Stremming \n"),
        # Gepland werk telt niet mee als storing.
        alert(alert_id="c", first_seen=epoch_at(today, 11), last_seen=epoch_at(today, 12),
              description="Oorzaak : Werkzaamheden \n"),
    ]
    overview = td.build_overview(rows, since.isoformat(), today.isoformat())

    summary = overview["summary"]
    assert summary["days_in_range"] == 3
    assert summary["incident_count"] == 2
    assert summary["planned_count"] == 1
    assert summary["days_with_disruption"] == 2
    # De middelste dag had niets, maar staat er wel in -- anders zou de
    # grafiek de rustige dagen wegpoetsen.
    assert [d["date"] for d in overview["per_day"]] == [
        since.isoformat(), (since + timedelta(days=1)).isoformat(), today.isoformat()
    ]
    assert overview["per_day"][1]["incidents"] == 0
    assert {c["label"] for c in overview["causes"]} == {"Ongeval", "Stremming"}
    assert overview["planned_causes"][0]["label"] == "Werkzaamheden"
    # Nieuwste eerst.
    assert overview["incidents"][0]["started_at"] == epoch_at(today, 9)


def test_build_overview_zonder_meldingen():
    today = date.today().isoformat()
    overview = td.build_overview([], today, today)
    assert overview["summary"]["incident_count"] == 0
    assert overview["summary"]["days_with_disruption_pct"] == 0.0
    assert overview["summary"]["longest_incident"] is None
    assert len(overview["per_day"]) == 1
    assert len(overview["per_weekday"]) == 7


# ── endpoint ─────────────────────────────────────────────────────────────

def test_api_tram_disruptions(client, temp_db, monkeypatch):
    from app import server

    monkeypatch.setattr(server._index, "tram_stop_ids", {"TRAMHALTE"}, raising=False)
    monkeypatch.setattr(server._index, "tram_route_ids", lambda: set())

    now = int(time.time())
    conn = temp_db.get_conn()
    conn.execute(
        """INSERT INTO alerts (alert_id, first_seen, last_seen, route_ids, stop_ids,
                               header, description, effect, cause, active)
           VALUES ('t1', ?, ?, '', 'TRAMHALTE', 'Wegens een tramstoring rijden de trams niet.',
                   'Oorzaak : Stroomstoring ', 'UNKNOWN_EFFECT', 'OTHER_CAUSE', 0)""",
        (now - 7200, now - 3600),
    )
    conn.commit()
    conn.close()

    data = client.get("/api/tram/disruptions?range=week").get_json()
    assert data["summary"]["incident_count"] == 1
    assert data["causes"][0]["label"] == "Stroomstoring"
    assert data["incidents"][0]["duration_seconds"] == 3600
    assert data["punctuality"] == []
