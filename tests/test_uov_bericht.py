"""Titel en tekst van U-OV-meldingen in de readerfeed (app/uov_bericht.py)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from app.uov_bericht import haltenamen, haltes_opsomming, oorzaak, titel_en_tekst

AMS = ZoneInfo("Europe/Amsterdam")
NU = datetime(2026, 11, 21, 12, 10, tzinfo=AMS).timestamp()
STOPS = {
    "1": {"name": "Utrecht, Rijnsweerd-Noord"}, "2": {"name": "Utrecht, Rijnsweerd-Zuid"},
    "3": {"name": "Utrecht, Padualaan"}, "4": {"name": "Utrecht, Heidelberglaan"},
    "5": {"name": "Utrecht, UMC Utrecht"}, "6": {"name": "Utrecht, WKZ/PMC"},
    "7": {"name": "Utrecht, P+R Science Park"},
    "11": {"name": "Utrecht, Rijnsweerd-Noord"}, "12": {"name": "Utrecht, Rijnsweerd-Zuid"},
    "20": {"name": "Rhenen, Rhenendael-West"}, "21": {"name": "Elst, Franseweg"},
}


def _ts(u, m, dag=21):
    return datetime(2026, 11, dag, u, m, tzinfo=AMS).timestamp()


def test_vage_trammelding_wordt_aangevuld():
    alert = {"stop_ids": ["1", "2", "3", "4", "5", "6", "11", "12", "7"], "cause": "ACCIDENT",
             "description": "Oorzaak : Ongeval \nEffect : Omleiding \nMaatregelen : Vervallen halte(n) \n"
                            "Tram: Bushalte tijdelijk vervallen. Plan uw reis via de reisplanner!\n",
             "valid_from": _ts(12, 15), "valid_until": _ts(17, 0)}
    titel, tekst = titel_en_tekst(alert, STOPS, "Tram: Bushalte tijdelijk vervallen. Plan uw reis via de reisplanner!",
                                  alert["description"], True, NU)
    assert titel == "Tram: 7 haltes tijdelijk vervallen, o.a. Rijnsweerd-Noord en Rijnsweerd-Zuid, door ongeval (tot 17:00)"
    assert "reisplanner" not in tekst and "Bushalte" not in tekst
    assert "Haltes: Rijnsweerd-Noord, Rijnsweerd-Zuid, Padualaan en nog 4 haltes." in tekst
    assert "Geldig 12:15–17:00." in tekst
    assert "Oorzaak : Ongeval" in tekst   # de velden blijven voor de reader


def test_duidelijke_kop_blijft_staan():
    kop = "U-OV: In verband met een ongeval vervallen momenteel de haltes Elst Franseweg en Rhenen Rhenendael-West."
    alert = {"stop_ids": ["20", "21"], "cause": "ACCIDENT", "description": "Oorzaak : onbekend \n" + kop,
             "valid_from": None, "valid_until": None}
    titel, tekst = titel_en_tekst(alert, STOPS, kop, alert["description"], False, NU)
    assert titel == kop[len("U-OV: "):].rstrip(".")
    assert "Haltes: Rhenen Rhenendael-West en Elst Franseweg." in tekst


def test_geen_dubbele_tijd_of_oorzaak():
    kop = "Lijn 9: halte vervallen tot 22:30. Plan je reis op u-ov.nl"
    alert = {"stop_ids": ["4"], "cause": "OTHER_CAUSE", "description": "Oorzaak : Voetbalwedstrijd \n" + kop,
             "valid_from": _ts(16, 0), "valid_until": _ts(22, 30)}
    titel, _ = titel_en_tekst(alert, STOPS, kop, alert["description"], False, NU)
    assert titel == "Lijn 9: halte vervallen tot 22:30 bij Heidelberglaan door voetbalwedstrijd"


def test_oorzaak_zonder_regeleinden():
    assert oorzaak({"description": "Oorzaak : onbekend Omleiding lijn 83 i.v.m. werkzaamheden. Effect :  Maatregelen :"}) == ""
    assert oorzaak({"description": "Oorzaak : Wisselstoring \nEffect : x", "cause": None}) == "Wisselstoring"
    assert oorzaak({"description": "", "cause": "ACCIDENT"}) == "Ongeval"


def test_haltes_meerdere_plaatsen_en_opsomming():
    assert haltenamen(["20", "21", "20"], STOPS) == ["Rhenen Rhenendael-West", "Elst Franseweg"]
    assert haltes_opsomming(["A"]) == "A"
    assert haltes_opsomming(["A", "B", "C"]) == "A, B en C"
    assert haltes_opsomming(["A", "B", "C", "D"]) == "A, B, C en nog 1 halte"
    assert haltes_opsomming(list("ABCDEFG")) == "A, B, C en nog 4 haltes"


def test_veel_haltes_geen_plaats_in_titel():
    stops = {str(i): {"name": f"Utrecht, Halte {i}"} for i in range(40)}
    alert = {"stop_ids": list(stops), "cause": "ACCIDENT", "description": "Oorzaak : Ongeval \n",
             "valid_from": None, "valid_until": None}
    titel, tekst = titel_en_tekst(alert, stops, "Tram rijdt niet", alert["description"], True, NU)
    assert titel == "Tram rijdt niet door ongeval"
    assert "en nog 37 haltes." in tekst


def test_eindtijd_andere_dag():
    alert = {"stop_ids": [], "cause": "CONSTRUCTION", "description": "", "valid_from": _ts(9, 0), "valid_until": _ts(5, 0, dag=23)}
    titel, tekst = titel_en_tekst(alert, STOPS, "Omleiding lijn 8", "", False, NU)
    assert titel == "Omleiding lijn 8 door werkzaamheden (tot 23 nov 05:00)"
    assert tekst == "Geldig 09:00–23 nov 05:00."
