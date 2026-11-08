"""Stakingsdagen herkennen in de U-OV-meldingen (tabel alerts).

U-OV kondigt een staking ruim vooraf aan, bv. "WOENSDAG 14 OKTOBER: STAKING
VAKBONDEN STREEKVERVOER – DE HELE DAG MINDER BUSSEN" of "Staking in
streekvervoer op 14/10". De handmatige lijst in service_events.py werd
vergeten bij 14 oktober; daarom zet de collector zulke dagen zelf in
staking_dagen. Een latere melding dat de staking "niet doorgaat" (zoals 22 okt)
haalt de dag weer weg."""
import datetime as dt
import re

MAANDEN = {
    "jan": 1, "januari": 1, "feb": 2, "februari": 2, "mrt": 3, "maart": 3, "apr": 4, "april": 4,
    "mei": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7, "aug": 8, "augustus": 8,
    "sep": 9, "sept": 9, "september": 9, "okt": 10, "oktober": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MAAND_RE = "|".join(sorted(MAANDEN, key=len, reverse=True))
_DATUM_WOORD = re.compile(rf"\b(\d{{1,2}})\s+({_MAAND_RE})\b\.?", re.IGNORECASE)
_DATUM_CIJFER = re.compile(r"\b(\d{1,2})/(\d{1,2})\b(?!/)")
_STAKING = re.compile(r"stak(ing|en|t)\b", re.IGNORECASE)
# Alleen stakingen die bus/tram raken (niet bv. alleen NS).
_OV = re.compile(r"bus|tram|streekvervoer|\bov\b|\bov-", re.IGNORECASE)
_AFGEBLAZEN = re.compile(r"gaat\s+niet\s+door|afgelast|ingetrokken|geannuleerd|opgeschort|toch\s+niet", re.IGNORECASE)


def _datums(tekst, gezien):
    """Datums (date) uit de tekst; het jaar volgt uit het moment van de melding
    (een melding in december over "5 januari" gaat over het volgende jaar)."""
    gevonden = []
    for dag, maand in [(int(d), MAANDEN[m.lower()]) for d, m in _DATUM_WOORD.findall(tekst)] + \
                      [(int(d), int(m)) for d, m in _DATUM_CIJFER.findall(tekst)]:
        for jaar in (gezien.year, gezien.year + 1):
            try:
                d = dt.date(jaar, maand, dag)
            except ValueError:
                break
            if d >= gezien - dt.timedelta(days=60):
                gevonden.append(d)
                break
    return gevonden


def staking_dagen_uit_meldingen(meldingen):
    """meldingen: iterabel van (alert_id, first_seen_epoch, header, description).
    Geeft (stakingsdagen, afgeblazen): stakingsdagen = {'YYYY-MM-DD': (alert_id, tekst)},
    afgeblazen = set van datums waarvoor een melding zegt dat de staking niet doorgaat."""
    dagen, afgeblazen = {}, set()
    for alert_id, first_seen, header, description in meldingen:
        tekst = f"{header or ''} {description or ''}"
        if not _STAKING.search(tekst):
            continue
        gezien = dt.datetime.fromtimestamp(first_seen).date()
        datums = _datums(tekst, gezien)
        if _AFGEBLAZEN.search(tekst):
            afgeblazen.update(d.isoformat() for d in datums)
            continue
        if not _OV.search(tekst):
            continue
        for d in datums:
            dagen.setdefault(d.isoformat(), (alert_id, (header or "").strip()))
    for d in afgeblazen:
        dagen.pop(d, None)
    return dagen, afgeblazen
