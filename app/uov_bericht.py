"""Titel en tekst van een U-OV-melding voor de readerfeed.

U-OV schrijft soms een kop die zonder context niets zegt ("Bushalte tijdelijk
vervallen. Plan uw reis via de reisplanner!"), terwijl de melding zelf wel
zegt welke haltes het zijn, wat de oorzaak is en tot wanneer het duurt. Een
korte kop (één zin) wordt met die gegevens aangevuld, alleen met wat er nog
niet in staat. Een langere kop zegt het meestal al en blijft zoals hij is, op
de standaardzinnen na.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .tram_disruptions import GTFS_CAUSE_LABELS

_AMSTERDAM = ZoneInfo("Europe/Amsterdam")

# Zoveel haltenamen noemen in de tekst; de rest wordt "en nog N haltes".
MAX_HALTES_GENOEMD = 3
# Boven dit aantal (verschillende) haltes zegt een plaats in de titel niets
# meer (hele lijn, staking).
MAX_HALTES_VOOR_PLAATS = 10
# Alleen een kop van één zin en hoogstens zoveel tekens wordt aangevuld.
MAX_KOP_VOOR_AANVULLEN = 70
# Een eindtijd verder weg dan dit is geen nuttige "tot" meer.
MAX_EINDTIJD_SECONDEN = 7 * 24 * 3600

# Zinnen die bij elke melding staan en niets zeggen.
_STANDAARDZIN_RE = re.compile(
    r"\s*\b(Plan (uw|je) reis|Raadpleeg de reisplanner|Download de [\w-]+[ -]app)[^!]*?(?:[.!](?=\s|$)|$)", re.IGNORECASE)
_UOV_PREFIX_RE = re.compile(r"^\s*U-OV:\s*", re.IGNORECASE)
_KAAL_VERVALLEN_RE = re.compile(r"^(bus)?halte(\(?s\)?)? (is |zijn )?(tijdelijk )?vervallen$", re.IGNORECASE)
# U-OV stuurt de velden soms zonder regeleinden ("Oorzaak : onbekend Omleiding ... Effect : ...").
_OORZAAK_RE = re.compile(r"Oorzaak\s*:\s*([^\n]*?)\s*(?=\n|\b(?:Effect|Maatregelen)\s*:|$)")
# De kop noemt al een oorzaak.
_OORZAAK_IN_KOP_RE = re.compile(
    r"\b(vanwege|wegens|i\.?v\.?m\.?|in verband met|door een|door de|als gevolg van|staking|gestaakt|ongeval|aanrijding|"
    r"storing|defect|werkzaamheden)\b", re.IGNORECASE)
# De kop noemt al een datum of tijd: dan geen "(tot ...)" erachter.
_TIJD_IN_KOP_RE = re.compile(
    r"(\d{1,2}[:.]\d{2}|\b(t/m|tot|vanaf|januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|"
    r"november|december|maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|vandaag|morgen)\b)", re.IGNORECASE)
_NIETSZEGGENDE_OORZAAK = {"", "onbekend", "niet gemeld", "overige oorzaak", "andere oorzaak", "anders", "overig"}
_MAANDEN = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def haltenamen(stop_ids, stops):
    """Verschillende haltenamen in volgorde van de melding (één halte telt per
    richting en per perron apart mee). GTFS schrijft "Plaats, Naam": liggen ze
    allemaal in één plaats, dan alleen de naam, anders "Plaats Naam"."""
    namen = []
    for sid in stop_ids:
        naam = (stops.get(sid) or {}).get("name")
        if naam and naam not in namen:
            namen.append(naam)
    plaatsen = {n.split(", ", 1)[0] for n in namen if ", " in n}
    uit = []
    for n in namen:
        if ", " in n:
            plaats, rest = n.split(", ", 1)
            n = rest if len(plaatsen) == 1 else f"{plaats} {rest}"
        if n not in uit:
            uit.append(n)
    return uit


def haltes_opsomming(namen):
    """"A, B, C en nog 4 haltes" (hoogstens MAX_HALTES_GENOEMD namen)."""
    if len(namen) <= MAX_HALTES_GENOEMD:
        return namen[0] if len(namen) == 1 else ", ".join(namen[:-1]) + " en " + namen[-1]
    rest = len(namen) - MAX_HALTES_GENOEMD
    return ", ".join(namen[:MAX_HALTES_GENOEMD]) + f" en nog {rest} {'halte' if rest == 1 else 'haltes'}"


def oorzaak(alert):
    """De oorzaak zoals U-OV die meldt, anders de GTFS-oorzaak; "" als die niets zegt."""
    m = _OORZAAK_RE.search(alert.get("description") or "")
    tekst = (m.group(1) if m else "").strip()
    # "onbekend Omleiding lijn 83 i.v.m. werkzaamheden.": U-OV zet soms de hele
    # tekst op de oorzaakregel. Dan de GTFS-oorzaak.
    if tekst.lower().startswith("onbekend") or len(tekst) > 30:
        tekst = ""
    tekst = tekst or GTFS_CAUSE_LABELS.get(alert.get("cause"), "")
    return "" if tekst.lower() in _NIETSZEGGENDE_OORZAAK else tekst


def _tijd(ts, nu):
    t = datetime.fromtimestamp(ts, _AMSTERDAM)
    if t.date() == datetime.fromtimestamp(nu, _AMSTERDAM).date():
        return t.strftime("%H:%M")
    return f"{t.day} {_MAANDEN[t.month - 1]} {t.strftime('%H:%M')}"


def _schoon(tekst, tram):
    tekst = _STANDAARDZIN_RE.sub("", tekst or "").strip()
    if tram:
        tekst = re.sub(r"\b([Bb])ushalte", lambda m: "Halte" if m.group(1) == "B" else "halte", tekst)
    return tekst


def titel_en_tekst(alert, stops, header, body, tram, nu):
    """(kern van de titel, beschrijving) voor de readerfeed.

    alert: de melding uit fetch_alerts() (stop_ids als lijst, cause, description,
    valid_from/valid_until); header/body: kop en tekst, al met "Tram: " gemarkeerd
    als het een trammelding is."""
    namen = haltenamen(alert.get("stop_ids") or [], stops)
    tot, van = alert.get("valid_until"), alert.get("valid_from")

    kop = _schoon(header, tram)
    voorvoegsel = ""
    if kop.startswith("Tram: "):
        voorvoegsel, kop = "Tram: ", kop[len("Tram: "):]
    kop = _UOV_PREFIX_RE.sub("", kop).strip()
    if not kop:
        kop = _UOV_PREFIX_RE.sub("", _schoon(body, tram)).strip()[:80]
    kop = kop.rstrip(" .!")
    if namen and _KAAL_VERVALLEN_RE.match(kop):
        kop = f"{len(namen)} haltes tijdelijk vervallen" if len(namen) > 1 else "Halte tijdelijk vervallen"

    if kop and len(kop) <= MAX_KOP_VOOR_AANVULLEN and not re.search(r"[.!?:]\s", re.sub(r"^Lijn \d+: ", "", kop)):
        basis = kop
        laag = kop.lower()
        # De volgorde van de haltes in de melding is willekeurig (beide richtingen
        # door elkaar), dus geen "X t/m Y" maar twee voorbeelden; de hele lijst
        # staat in de tekst.
        if namen and len(namen) <= MAX_HALTES_VOOR_PLAATS and not any(n.lower() in laag for n in namen):
            if len(namen) == 1:
                kop += f" bij {namen[0]}"
            elif len(namen) == 2:
                kop += f" bij {namen[0]} en {namen[1]}"
            else:
                kop += f", o.a. {namen[0]} en {namen[1]},"
        o = oorzaak(alert)
        if o and not _OORZAAK_IN_KOP_RE.search(basis):
            kop += f" door {o.lower()}"
        kop = kop.rstrip(",")
        if tot and nu < tot <= nu + MAX_EINDTIJD_SECONDEN and not _TIJD_IN_KOP_RE.search(basis):
            kop += f" (tot {_tijd(tot, nu)})"
    titel = voorvoegsel + kop

    # Beschrijving: de velden van U-OV (Oorzaak/Effect/Maatregelen) blijven staan,
    # de kopregel en standaardzinnen gaan eruit, haltes en tijd komen erbij.
    tekst = body or ""
    if header and header in tekst:
        tekst = tekst.replace(header, "")
    tekst = _schoon(tekst, tram)
    delen = [tekst] if tekst else []
    if namen:
        delen.append(f"{'Haltes' if len(namen) > 1 else 'Halte'}: {haltes_opsomming(namen)}.")
    if van and tot and tot <= nu + MAX_EINDTIJD_SECONDEN:
        delen.append(f"Geldig {_tijd(van, nu)}–{_tijd(tot, nu)}.")
    return titel, " ".join(delen)
