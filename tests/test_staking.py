"""Stakingsdagen uit de U-OV-meldingen (app/staking.py) en de koppeling met
de bijzondere dagen (service_events)."""
import datetime as dt
import time

from app import collector, service_events
from app.staking import staking_dagen_uit_meldingen


def ts(s):
    return int(dt.datetime.fromisoformat(s).timestamp())


ECHTE_MELDINGEN = [
    ("a", ts("2026-09-05T10:00"), "9 SEPTEMBER: STAKING VAKBONDEN – DE HELE DAG MINDER BUSSEN EN GEEN TREINEN", ""),
    ("b", ts("2026-09-08T10:00"), "9 sept: landelijke ov- staking minder/geen bussen en trams.", ""),
    ("c", ts("2026-10-13T10:00"), "Let op: Staking in streekvervoer op 14/10. Kijk op onze website", ""),
    ("d", ts("2026-10-12T10:00"), "U-OV: WOENSDAG 14 OKTOBER: STAKING VAKBONDEN STREEKVERVOER – DE HELE DAG MINDER BUSSEN", ""),
    ("e", ts("2026-10-20T10:00"), "allGo: DONDERDAG 22 OKTOBER: STAKING VAKBONDEN STREEKVERVOER – DE HELE DAG MINDER BUSSEN", ""),
    ("f", ts("2026-10-21T10:00"), "LET OP! De door de vakbonden aangekondigde staking in het OV op donderdag 22 oktober gaat niet door.", ""),
    ("g", ts("2026-09-09T08:00"), "9 sept: vanwege de landelijke staking rijden tramlijnen 20 en 22 niet. Lijn 21 rijdt", ""),
]


def test_herkent_echte_meldingen_en_afgeblazen_staking():
    dagen, afgeblazen = staking_dagen_uit_meldingen(ECHTE_MELDINGEN)
    assert set(dagen) == {"2026-09-09", "2026-10-14"}
    assert afgeblazen == {"2026-10-22"}


def test_jaarwissel_en_alleen_ns():
    dagen, _ = staking_dagen_uit_meldingen([
        ("x", ts("2026-12-20T10:00"), "Staking op 5 januari: minder bussen", ""),
        ("y", ts("2026-10-01T10:00"), "NS-staking op 3 oktober: geen treinen", ""),
        ("z", ts("2026-10-01T10:00"), "Omleiding lijn 5 op 3 oktober", ""),
    ])
    assert set(dagen) == {"2027-01-05"}


def test_collector_zet_dagen_in_tabel_en_haalt_afgeblazen_weg(temp_db, monkeypatch):
    conn = temp_db.get_conn()
    for alert_id, first_seen, header, description in ECHTE_MELDINGEN[:5]:
        conn.execute(
            "INSERT INTO alerts (alert_id, first_seen, last_seen, header, description, active) VALUES (?, ?, ?, ?, ?, 0)",
            (alert_id, first_seen, first_seen, header, description),
        )
    conn.commit()
    collector.detect_strike_days_job()
    rows = {r[0] for r in conn.execute("SELECT date FROM staking_dagen")}
    assert rows == {"2026-09-09", "2026-10-14", "2026-10-22"}
    a = ECHTE_MELDINGEN[5]
    conn.execute(
        "INSERT INTO alerts (alert_id, first_seen, last_seen, header, description, active) VALUES (?, ?, ?, ?, ?, 0)",
        (a[0], a[1], a[1], a[2], a[3]),
    )
    conn.commit()
    collector.detect_strike_days_job()
    rows = {r[0] for r in conn.execute("SELECT date FROM staking_dagen")}
    assert rows == {"2026-09-09", "2026-10-14"}
    conn.close()

    # De bijzondere dagen nemen de automatische dag over; handmatig gaat voor.
    monkeypatch.setattr(service_events, "KNOWN_SERVICE_EVENTS", [
        {"date": "2026-09-09", "operators": ["Keolis"], "label": "Staking", "description": "handmatig"}])
    service_events._auto.update(tijd=0.0, lijst=[])
    dagen = {e["date"]: e for e in service_events.all_events()}
    assert dagen["2026-09-09"]["description"] == "handmatig"
    assert dagen["2026-10-14"]["operators"] == ["Transdev", "Keolis"]
    assert ("2026-10-14", "Transdev") in service_events.excluded_operator_dates()
    service_events._auto.update(tijd=0.0, lijst=[])
