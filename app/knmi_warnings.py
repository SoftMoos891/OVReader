"""Tijdsaanduiding voor KNMI-weerwaarschuwingen in de RSS-meldingen.

Sinds 2 okt 2026 haalt de DVZ RSS-bridge de waarschuwingen bij het KNMI
(rssbridge/knmi_ophalen.py) en vraagt de collector ze daar op (bridge_weer.py).
De eigen ophaal- en leescode van dit bestand is toen verwijderd; de tests
daarvan staan nu in de bridge (rssbridge/tests/test_knmi_ophalen.py). Wat
overblijft is format_active_from, voor de titel en tekst van de RSS-melding.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def format_active_from(active_from_iso, now=None):
    """Mens-leesbare, dagrelatieve tijdsaanduiding voor active_from (bv.
    "vandaag 14:00 uur", "morgen 09:00 uur", "wo 09:00 uur") -- gebruikt in
    de RSS-titel/-tekst zodat een nog niet actieve waarschuwing niet leest
    alsof die al geldt."""
    now = now or datetime.now(timezone.utc)
    dt = datetime.fromisoformat(active_from_iso).astimezone(_AMSTERDAM)
    now_local = now.astimezone(_AMSTERDAM)
    delta_days = (dt.date() - now_local.date()).days
    time_str = dt.strftime("%H:%M")
    if delta_days == 0:
        return f"vandaag {time_str} uur"
    if delta_days == 1:
        return f"morgen {time_str} uur"
    weekdays = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    return f"{weekdays[dt.weekday()]} {time_str} uur"
