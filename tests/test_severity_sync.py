"""Bewaakt dat de ernst-detectie in Python en JavaScript gelijk blijft.

Dezelfde regels bestaan noodgedwongen twee keer: static/js/severity.js
bepaalt in de browser welke melding een rode badge krijgt, en
app/collector.py bepaalt in een los proces (zonder browser) welke melding
in de RSS-feed komt. Dat kon tot nu toe alleen met een comment ("bewust in
sync houden") bewaakt worden -- deze test leest de JS-bron in en vergelijkt
'm met de Python-kant, zodat de twee niet ongemerkt uit elkaar lopen.

Faalt deze test na een bewuste wijziging? Pas dan de ándere kant ook aan;
dat is precies wat 'ie moet afdwingen.
"""
import json
import re
from pathlib import Path

import pytest

from app.collector import (
    SEVERE_ALERT_CAUSES,
    SEVERE_ALERT_KEYWORDS,
    SEVERE_ALERT_MIN_STOPS,
    _is_severe_alert,
)

SEVERITY_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "severity.js"


@pytest.fixture(scope="module")
def js_source():
    assert SEVERITY_JS.exists(), f"{SEVERITY_JS} ontbreekt"
    return SEVERITY_JS.read_text(encoding="utf-8")


def _js_array(source, name):
    """Leest een `const <naam> = [ ... ];`-array uit de JS-bron."""
    match = re.search(rf"const {name} = \[(.*?)\];", source, re.S)
    assert match, f"{name} niet gevonden in severity.js"
    return [m.group(1) for m in re.finditer(r"'([^']*)'", match.group(1))]


def _js_number(source, name):
    """Leest een `const <naam> = <getal>;` uit de JS-bron."""
    match = re.search(rf"const {name} = (\d+);", source)
    assert match, f"{name} niet gevonden in severity.js"
    return int(match.group(1))


def _js_object_keys(source, name):
    """Leest de sleutels van een `const <naam> = {{ ... }};`-object."""
    match = re.search(rf"const {name} = \{{(.*?)\}};", source, re.S)
    assert match, f"{name} niet gevonden in severity.js"
    return {m.group(1) for m in re.finditer(r"(\w+):", match.group(1))}


def test_keyword_lists_are_identical(js_source):
    """Zelfde woorden én zelfde volgorde: 'verstoring' moet vóór 'storing'
    staan, anders levert de JS-kant het verkeerde label op (de eerste match
    wint, en 'verstoring' bevat de substring 'storing')."""
    assert _js_array(js_source, "SEVERE_ALERT_KEYWORDS") == SEVERE_ALERT_KEYWORDS


def test_cause_lists_are_identical(js_source):
    """De JS-kant heeft labels bij de oorzaken en Python alleen de namen --
    de verzameling oorzaken die als ernstig telt moet wel gelijk zijn."""
    assert _js_object_keys(js_source, "SEVERE_CAUSE_LABELS") == SEVERE_ALERT_CAUSES


def test_verstoring_is_checked_before_storing(js_source):
    """Los van de vergelijking hierboven: deze volgorde-eis is subtiel genoeg
    om apart vast te leggen."""
    for keywords in (SEVERE_ALERT_KEYWORDS, _js_array(js_source, "SEVERE_ALERT_KEYWORDS")):
        assert keywords.index("verstoring") < keywords.index("storing")


def test_stremming_is_not_a_keyword(js_source):
    """Stond er ooit in en gaf massaal valse urgente meldingen: elke
    routinematige halteverplaatsing draagt "Oorzaak : Stremming" in zijn
    beschrijving. Hoort aan geen van beide kanten terug te komen."""
    assert "stremming" not in SEVERE_ALERT_KEYWORDS
    assert "stremming" not in _js_array(js_source, "SEVERE_ALERT_KEYWORDS")


def test_python_and_js_agree_on_a_set_of_example_alerts(js_source):
    """Vergelijkt het daadwerkelijke oordeel, niet alleen de lijsten -- zo
    valt ook een wijziging in de logica zelf op."""


    voorbeelden = [
        ({"header": "i.v.m. een ongeval rijden er geen trams", "description": "",
          "cause": "OTHER_CAUSE"}, True),
        ({"header": "halte vervalt i.v.m. werkzaamheden", "description": "",
          "cause": "OTHER_CAUSE"}, False),
        ({"header": "halte vervalt", "description": "Oorzaak : Stremming Effect : Omleiding",
          "cause": "OTHER_CAUSE"}, False),
        ({"header": "melding", "description": "", "cause": "POLICE_ACTIVITY"}, True),
        ({"header": "melding", "description": "", "cause": "MAINTENANCE"}, False),
    ]

    keywords = _js_array(js_source, "SEVERE_ALERT_KEYWORDS")
    causes = _js_object_keys(js_source, "SEVERE_CAUSE_LABELS")
    # Ruim boven de omvangdrempel: deze test gaat over de woord-/oorzaak-
    # herkenning, niet over de omvang (die heeft zijn eigen tests hieronder).
    groot = SEVERE_ALERT_MIN_STOPS
    for alert, verwacht_ernstig in voorbeelden:
        python_oordeel = _is_severe_alert(
            alert["header"], alert["description"], alert["cause"], stop_count=groot
        )
        tekst = f"{alert['header']} {alert['description']}".lower()
        js_oordeel = any(k in tekst for k in keywords) or alert["cause"] in causes

        assert python_oordeel == verwacht_ernstig, alert
        assert js_oordeel == verwacht_ernstig, alert


def test_threshold_is_identical(js_source):
    """De omvangdrempel staat aan beide kanten hard gecodeerd; loopt die uit
    elkaar, dan verschilt de rode badge op het dashboard van wat er in de
    RSS-feed belandt."""
    assert _js_number(js_source, "SEVERE_ALERT_MIN_STOPS") == SEVERE_ALERT_MIN_STOPS


def test_small_accident_is_not_severe():
    """De aanleiding voor de omvangdrempel: "Halte vervalt tot nader order
    i.v.m een ongeval" raakte 4 stop_ids (2 haltes, beide richtingen) en
    kreeg toch dezelfde rode badge als een tramlijn die stillag."""
    assert not _is_severe_alert(
        "Halte vervalt tot nader order i.v.m een ongeval.",
        "Oorzaak : Ongeval Effect : Omleiding Maatregelen : Vervallen halte(n)",
        "OTHER_CAUSE",
        stop_count=4,
    )


def test_large_accident_is_still_severe():
    """Het tegenvoorbeeld uit dezelfde week: geen trams tussen P+R Westraven
    en Jaarbeursplein, 16 stop_ids. Moet urgent blijven."""
    assert _is_severe_alert(
        "U-OV: i.v.m. een ongeval rijden er geen trams tussen P+R Westraven en Jaarbeursplein.",
        "Oorzaak : Ongeval Effect : Omleiding",
        "OTHER_CAUSE",
        stop_count=16,
    )


def test_line_level_alert_bypasses_the_stop_threshold():
    """Een melding die op lijnniveau is aangemeld is per definitie breder dan
    een paar losse haltes, ook als er weinig stop_ids bij staan."""
    assert _is_severe_alert(
        "Lijn 12 rijdt niet i.v.m. een aanrijding.", "", "OTHER_CAUSE",
        stop_count=0, route_count=1,
    )


def test_magnitude_alone_is_not_enough():
    """Omvang vervangt het trefwoord niet: de optocht-melding op lijn 81 raakt
    ~20 haltes maar blijft een geplande, routinematige situatie."""
    assert not _is_severe_alert(
        "Lijn 81: halte vervallen tot 17:00.",
        "Oorzaak : Optocht Effect : Omleiding",
        "OTHER_CAUSE",
        stop_count=20,
    )
