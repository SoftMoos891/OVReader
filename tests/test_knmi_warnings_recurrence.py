from unittest.mock import patch

from app import collector


def _yellow_warning(phenomenon_id, active_from_iso, is_current):
    return [{
        "phenomenon_id": phenomenon_id,
        "phenomenon_label": "Onweer",
        "color": "YELLOW",
        "color_label": "Geel",
        "active_from": active_from_iso,
        "is_current": is_current,
        "worst_at": active_from_iso,
        "header": "Testkop",
        "description": "Testomschrijving.",
    }]


def test_new_occurrence_reopens_rss_item_after_previous_one_resolved(temp_db, monkeypatch):
    # Reproduceert de bug van 31 aug: dezelfde fenomeen+kleur-combinatie
    # (dus dezelfde guid) komt een tweede keer voor nadat de eerste al als
    # 'voorbij' was gemarkeerd -- de nieuwe waarschuwing moet gewoon weer
    # als open item verschijnen, niet stilzwijgend wegvallen door INSERT OR
    # IGNORE tegen de allang bestaande (opgeloste) rij.
    monkeypatch.setenv("KNMI_API_KEY", "dummy")

    with patch.object(collector, "fetch_utrecht_warnings", return_value=_yellow_warning("LT", "2026-08-20T09:00:00+00:00", False)):
        collector.fetch_knmi_warnings_job()

    with patch.object(collector, "fetch_utrecht_warnings", return_value=[]):
        collector.fetch_knmi_warnings_job()

    conn = temp_db.get_conn()
    row = conn.execute("SELECT * FROM rss_feed_items WHERE guid='knmi-warning-LT-YELLOW'").fetchone()
    conn.close()
    assert row["resolved_at"] is not None

    with patch.object(collector, "fetch_utrecht_warnings", return_value=_yellow_warning("LT", "2026-08-31T12:00:00+02:00", False)):
        collector.fetch_knmi_warnings_job()

    conn = temp_db.get_conn()
    row = conn.execute("SELECT * FROM rss_feed_items WHERE guid='knmi-warning-LT-YELLOW'").fetchone()
    conn.close()
    assert row["resolved_at"] is None
    assert "vanaf" in row["title"]
