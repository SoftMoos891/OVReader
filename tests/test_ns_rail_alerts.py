from app.ns_rail_alerts import _title_with_cause


def test_oorzaak_voor_het_traject():
    d = {"title": "Schiphol - Lelystad; Schiphol - Utrecht.",
         "timespans": [{"cause": {"label": "defecte bovenleiding"}}]}
    assert _title_with_cause(d) == "Defecte bovenleiding: Schiphol - Lelystad; Schiphol - Utrecht."


def test_werkzaamheden_en_zonder_oorzaak_blijft_het_traject():
    assert _title_with_cause({"title": "Utrecht - Gouda.", "timespans": [{"cause": {"label": "werkzaamheden"}}]}) == "Utrecht - Gouda."
    assert _title_with_cause({"title": "Utrecht - Gouda.", "timespans": [{}]}) == "Utrecht - Gouda."
    assert _title_with_cause({"title": "Utrecht - Gouda."}) == "Utrecht - Gouda."
