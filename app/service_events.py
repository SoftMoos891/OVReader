"""Handmatig bijgehouden lijst van bekende, niet-representatieve
verstoringsdagen (stakingen e.d.). Zulke dagen blijven zichtbaar in de
gewone uitvalcijfers (/uitval) -- dat is wat er die dag daadwerkelijk
gebeurde -- maar worden uitgesloten van de langjarige trends en records op
/trends, omdat één stakingsdag anders de records en het langetermijngemiddelde
blijvend zou vertekenen.

Nieuw evenement toevoegen: gewoon een regel aan KNOWN_SERVICE_EVENTS
toevoegen, geen verdere code nodig."""

KNOWN_SERVICE_EVENTS = [
    {
        "date": "2026-09-09",
        "operators": ["Transdev", "Keolis"],
        "label": "Staking",
        "description": "Staking bij Transdev en Keolis.",
    },
]


def events_in_range(since_date, until_date):
    """Bekende evenementen die (deels) binnen [since_date, until_date] vallen."""
    return [e for e in KNOWN_SERVICE_EVENTS if since_date <= e["date"] <= until_date]


def event_for(service_date):
    for e in KNOWN_SERVICE_EVENTS:
        if e["date"] == service_date:
            return e
    return None


def excluded_operator_dates():
    """Set van (date, operator)-paren die uit trend-/record-berekeningen
    gehouden moeten worden."""
    return {
        (e["date"], operator)
        for e in KNOWN_SERVICE_EVENTS
        for operator in e["operators"]
    }
