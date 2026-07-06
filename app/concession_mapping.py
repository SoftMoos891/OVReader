"""
Onderscheid tussen de twee concessies die beide onder de merknaam U-OV rijden
(per 14-12-2025, looptijd 2026-2035):

- Concessie Utrecht Buiten  -> Keolis   (Amersfoort stadsdienst, Woerden,
  Soest, Veenendaal, streeklijnen/U-liner, nachtlijnen N2/N3/N7/N8/N17/N20/
  N26/N41/N50/N70/N76, buurtbussen 5xx/6xx)
- Concessie Utrecht Binnen  -> Transdev (Utrecht stadsdienst, Vianen,
  Bilthoven, U-tram 20/21/22, streeklijnen, U-flex, nachtlijnen N1/N4/N5/N6/N9)

Bron: https://wiki.ovinnederland.nl/wiki/Concessie_Utrecht_Buiten_(2026-2035)
      https://wiki.ovinnederland.nl/wiki/Concessie_Utrecht_Binnen_(2026-2035)

GTFS kent geen apart agency_id per sub-concessie (beide draaien onder
agency_id "UOV"), dus dit onderscheid staat niet in de brondata zelf en wordt
hier hardcoded bijgehouden. Lijnnummers zijn NIET uniek tussen de concessies
(zowel Amersfoort als Utrecht hebben bijvoorbeeld een lijn "1", "7", "8" voor
hun eigen stadsdienst) -- daarom wordt voor overlappende nummers op
sleutelwoorden in de routebeschrijving gematcht in plaats van alleen het
lijnnummer.

Bijwerken: als er een nieuwe lijn bijkomt die hier niet in voorkomt, geeft
build_static_index.py een waarschuwing ("Onbekend") met het lijnnummer en de
routebeschrijving -- voeg 'm dan hier toe.

Modaliteit: Transdev exploiteert zowel bus als de U-tram (20/21/22, GTFS
route_type 0) -- die worden als aparte "operator"-waarden teruggegeven
("Transdev bus" resp. "Transdev tram") zodat de UI ze uit elkaar kan houden.
Keolis rijdt geen tram in deze concessie, dus die blijft gewoon "Keolis".
"""

KEOLIS = "Keolis"
TRANSDEV_BUS = "Transdev bus"
TRANSDEV_TRAM = "Transdev tram"
UNKNOWN = "Onbekend"

# U-tram 20/21/22 (Utrecht Binnen, Transdev) -- lightrail, geen bus. Apart
# gehouden van UNAMBIGUOUS_TRANSDEV hieronder zodat de UI een expliciet
# onderscheid kan maken tussen "Transdev bus" en "Transdev tram".
TRAM_LINES_TRANSDEV = {"20", "21", "22"}

# Lijnnummers die uitsluitend bij Utrecht Buiten (Keolis) horen.
UNAMBIGUOUS_KEOLIS = {
    "A", "B", "M",
    "15", "17", "56", "58", "59", "70", "71", "80", "82", "83", "87",
    "102", "106", "117", "120", "121", "123", "195", "203", "207",
    "272", "298", "299",
    "302", "307", "315", "326", "330", "341", "350", "376", "380", "395",
    "501", "503", "505", "522", "523", "524", "526", "572", "573", "575",
    "603", "680", "682", "683", "695",
    "N2", "N3", "N7", "N8", "N17", "N20", "N26", "N41", "N50", "N70", "N76",
}

# Lijnnummers die uitsluitend bij Utrecht Binnen (Transdev) horen.
UNAMBIGUOUS_TRANSDEV = {
    "11", "12", "13", "16", "18", "27", "28", "29", "30", "31", "32", "34",
    "43", "44", "47", "48", "55", "57", "64", "65", "66", "73", "74", "77",
    "81", "84", "85", "90", "111", "122", "128", "158", "184", "565",
    "920", "921",  # tramvervangend vervoer voor U-tram 20/21
    "N1", "N4", "N5", "N6", "N9",
    "Flex",  # alle huidige U-Flex-zones onder de naam "Flex" zijn Binnen; Buiten gebruikt A/B/M
}

# Lijnnummers die bij BEIDE concessies voorkomen (elke stad nummert zijn eigen
# stadsdienst vanaf 1) -- hier matchen we op een kenmerkend woord uit de
# GTFS long_name om te bepalen welke concessie het is.
AMBIGUOUS_KEYWORDS = {
    "1": (["amersfoort"], KEOLIS),
    "2": (["amersfoort"], KEOLIS),
    "3": (["amersfoort", "molenvliet"], KEOLIS),
    "4": (["amersfoort", "molenvliet", "veenendaal", "ede"], KEOLIS),
    "5": (["amersfoort", "woerden", "montfoort"], KEOLIS),
    "6": (["amersfoort"], KEOLIS),
    "7": (["amersfoort"], KEOLIS),
    "8": (["amersfoort"], KEOLIS),
    "9": (["amersfoort"], KEOLIS),
    "10": (["amersfoort"], KEOLIS),
    "19": (["amersfoort", "rusthof"], KEOLIS),
}


def classify_operator(short_name, long_name):
    """Bepaalt of een lijn bij Keolis (Utrecht Buiten), Transdev-bus of
    Transdev-tram (beide Utrecht Binnen) hoort. Geeft UNKNOWN terug als de
    lijn niet herkend wordt (nieuwe lijn sinds het laatste onderhoud van dit
    bestand)."""
    if short_name in TRAM_LINES_TRANSDEV:
        return TRANSDEV_TRAM
    if short_name in UNAMBIGUOUS_KEOLIS:
        return KEOLIS
    if short_name in UNAMBIGUOUS_TRANSDEV:
        return TRANSDEV_BUS
    if short_name in AMBIGUOUS_KEYWORDS:
        keywords, match_operator = AMBIGUOUS_KEYWORDS[short_name]
        other_operator = TRANSDEV_BUS if match_operator == KEOLIS else KEOLIS
        long_lower = (long_name or "").lower()
        if any(kw in long_lower for kw in keywords):
            return match_operator
        return other_operator
    return UNKNOWN
