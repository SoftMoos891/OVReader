"""Regels voor wegsituaties: welke soorten urgent zijn en een melding geven.

Sinds 2 okt 2026 maakt de DVZ RSS-bridge de wegsituaties (rssbridge/
wegsituaties.js met wegsituaties_ophalen.py, een kopie van de parser die hier
stond) en vraagt de collector de lijst daar op (bridge_wegen.py). De eigen
download- en leescode van dit bestand is toen verwijderd; de tests daarvan
staan nu in de bridge (rssbridge/tests/test_wegsituaties_ophalen.py). Wil je
iets veranderen aan welke situaties er komen of hoe ze heten, doe dat in de
bridge. Hier blijven alleen de regels die de collector en de pagina's van de OV
Reader zelf gebruiken.
"""

# Deze typen tellen als "ernstig" voor de meldingenlijst/kaart op de site --
# de rest is vooral routine wegwerkzaamheden/snelheidsmaatregelen, geen
# incident. VehicleObstruction leek eerst een logische toevoeging, maar
# bleek in de praktijk (provincie Utrecht, steekproef) voor 78% gewoon een
# "Pijlwagen" (mobiele wegwerk-begeleiding) te zijn -- geen incident, dus
# bewust NIET meegenomen. Zelfde les als bij de ADDITIONAL_SERVICE-
# blanket-regel voor bus-meldingen. GeneralObstruction (bv. lading/object op
# de weg) is wel een echt incident.
SEVERE_ROAD_TYPES = {"Accident", "AbnormalTraffic", "GeneralObstruction"}

# Smallere subset voor de RSS-feed: GeneralObstruction mag wel op de site
# getoond worden (zie hierboven), maar hoeft geen RSS-melding te geven -- op
# verzoek, waarschijnlijk omdat een obstakel op de weg minder vaak een echt
# meldingswaardig incident is dan een ongeval/abnormale verkeersdrukte.
RSS_ROAD_TYPES = {"Accident", "AbnormalTraffic"}

# DATEX II overallSeverity-waarde die overeenkomt met "Gering" in de UI (zie
# ROAD_SEVERITY_LABELS in templates/index.html en lite.html) -- op verzoek
# nooit urgent en nooit een RSS-melding, ongeacht het record_type.
NEGLIGIBLE_SEVERITY = "lowest"

# Demonstraties komen in de NDW-feed vrijwel altijd binnen met een op zich
# onschuldig record_type (in de praktijk RoadOrCarriagewayOrLaneManagement,
# "Wegwerkzaamheden") terwijl een demonstratie wel degelijk tot afsluitingen/
# omleidingen kan leiden -- op verzoek daarom altijd urgent, los van
# record_type, via het cause-veld (zie ovreader_road_situations_cause_gap
# memory: dit veld werd al geparsed/getoond, maar telde nog niet mee in de
# urgentie-logica).
DEMONSTRATION_CAUSE = "demonstratie"


def is_demonstration(cause):
    return bool(cause) and DEMONSTRATION_CAUSE in cause.lower()
