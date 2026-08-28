from unittest.mock import patch

from app import collector


def _yellow_warning(active_from_iso, is_current):
    return [{
        "phenomenon_id": "WS",
        "phenomenon_label": "Wind",
        "color": "YELLOW",
        "color_label": "Geel",
        "active_from": active_from_iso,
        "is_current": is_current,
        "worst_at": active_from_iso,
        "header": "Testkop",
        "description": "Testomschrijving.",
    }]


def test_started_rss_item_appears_when_warning_becomes_current(temp_db, monkeypatch):
    monkeypatch.setenv("KNMI_API_KEY", "dummy")

    with patch.object(collector, "fetch_utrecht_warnings", return_value=_yellow_warning("2026-08-29T09:00:00+00:00", False)):
        collector.fetch_knmi_warnings_job()

    conn = temp_db.get_conn()
    rows = {r["guid"]: r for r in conn.execute("SELECT * FROM rss_feed_items").fetchall()}
    assert "knmi-warning-WS-YELLOW" in rows
    assert "knmi-warning-WS-YELLOW-started" not in rows
    conn.close()

    with patch.object(collector, "fetch_utrecht_warnings", return_value=_yellow_warning("2026-08-29T09:00:00+00:00", True)):
        collector.fetch_knmi_warnings_job()

    conn = temp_db.get_conn()
    rows = {r["guid"]: r for r in conn.execute("SELECT * FROM rss_feed_items").fetchall()}
    conn.close()
    assert "knmi-warning-WS-YELLOW-started" in rows
    assert "is begonnen" in rows["knmi-warning-WS-YELLOW-started"]["title"]
    assert rows["knmi-warning-WS-YELLOW-started"]["resolved_at"] is None


def test_no_started_rss_item_when_warning_is_current_from_the_start(temp_db, monkeypatch):
    monkeypatch.setenv("KNMI_API_KEY", "dummy")

    with patch.object(collector, "fetch_utrecht_warnings", return_value=_yellow_warning("2026-08-29T09:00:00+00:00", True)):
        collector.fetch_knmi_warnings_job()

    conn = temp_db.get_conn()
    rows = {r["guid"]: r for r in conn.execute("SELECT * FROM rss_feed_items").fetchall()}
    conn.close()
    assert "knmi-warning-WS-YELLOW" in rows
    assert "knmi-warning-WS-YELLOW-started" not in rows
