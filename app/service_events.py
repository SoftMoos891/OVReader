"""Handmatig bijgehouden lijst van bekende, niet-representatieve
verstoringsdagen (stakingen e.d.). Zulke dagen blijven zichtbaar in de
gewone uitvalcijfers (/uitval) -- dat is wat er die dag daadwerkelijk
gebeurde -- maar worden uitgesloten van de langjarige trends en records op
/trends, omdat één stakingsdag anders de records en het langetermijngemiddelde
blijvend zou vertekenen.

Nieuw evenement toevoegen: gewoon een regel aan KNOWN_SERVICE_EVENTS
toevoegen, geen verdere code nodig. Stakingen die U-OV vooraf meldt, vindt de
collector ook zelf (app/staking.py, tabel staking_dagen)."""

import sqlite3
import time

from . import db

KNOWN_SERVICE_EVENTS = [
    {
        "date": "2026-09-09",
        "operators": ["Transdev", "Keolis"],
        "label": "Staking",
        "description": "Staking bij Transdev en Keolis.",
    },
    {
        "date": "2026-10-14",
        "operators": ["Transdev", "Keolis"],
        "label": "Staking",
        "description": "Staking in het streekvervoer: de hele dag minder bussen en trams.",
    },
]


# Stakingsdagen die de collector zelf in de U-OV-meldingen vond (tabel
# staking_dagen, zie app/staking.py). Tien minuten bewaard: de lijst verandert
# hooguit een paar keer per jaar.
_AUTO_TTL = 600
_auto = {"tijd": 0.0, "lijst": []}


def _auto_events():
    if time.time() - _auto["tijd"] < _AUTO_TTL:
        return _auto["lijst"]
    try:
        conn = db.get_conn()
        try:
            rows = conn.execute("SELECT date, label, description FROM staking_dagen ORDER BY date").fetchall()
        finally:
            conn.close()
        lijst = [{"date": r["date"], "operators": ["Transdev", "Keolis"], "label": r["label"],
                  "description": (r["description"] or "") + " (herkend in de U-OV-meldingen)", "auto": True}
                 for r in rows]
    except sqlite3.Error:
        lijst = _auto["lijst"]
    _auto.update(tijd=time.time(), lijst=lijst)
    return lijst


def all_events():
    """Handmatige lijst plus de automatisch herkende dagen (handmatig gaat voor)."""
    handmatig = {e["date"] for e in KNOWN_SERVICE_EVENTS}
    return KNOWN_SERVICE_EVENTS + [e for e in _auto_events() if e["date"] not in handmatig]


def events_in_range(since_date, until_date):
    """Bekende evenementen die (deels) binnen [since_date, until_date] vallen."""
    return sorted((e for e in all_events() if since_date <= e["date"] <= until_date), key=lambda e: e["date"])


def event_for(service_date):
    for e in all_events():
        if e["date"] == service_date:
            return e
    return None


def excluded_operator_dates():
    """Set van (date, operator)-paren die uit trend-/record-berekeningen
    gehouden moeten worden."""
    return {
        (e["date"], operator)
        for e in all_events()
        for operator in e["operators"]
    }
