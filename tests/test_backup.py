import gzip
import sqlite3
from datetime import date

from app import collector


def _insert_history(temp_db):
    today = date.today().isoformat()
    conn = temp_db.get_conn()
    conn.execute(
        "INSERT INTO trips_ran_daily (service_date, trip_id, route_id) VALUES (?, 't1', 'TESTROUTE')",
        (today,),
    )
    conn.execute(
        """INSERT INTO trip_cancellations
           (trip_id, service_date, route_id, start_time, first_seen, last_seen)
           VALUES ('c1', ?, 'TESTROUTE', '08:00:00', 0, 0)""",
        (today,),
    )
    conn.commit()
    conn.close()


def test_backup_contains_history_tables(temp_db, tmp_path):
    _insert_history(temp_db)

    collector.backup_history()

    backup_dir = temp_db.DB_PATH.parent / "backups"
    backups = sorted(backup_dir.glob("history_*.db.gz"))
    assert len(backups) == 1

    # Uitpakken en controleren dat de data er echt in zit.
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(backups[0].read_bytes()))
    conn = sqlite3.connect(restored)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert set(collector.BACKUP_TABLES) <= tables
        assert conn.execute("SELECT COUNT(*) FROM trip_cancellations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM trips_ran_daily").fetchone()[0] == 1
    finally:
        conn.close()

    # Het tijdelijke ongecomprimeerde bestand mag niet blijven slingeren.
    assert not (backup_dir / "history_tmp.db").exists()


def test_backup_rotation_keeps_only_newest(temp_db):
    _insert_history(temp_db)
    backup_dir = temp_db.DB_PATH.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    # Oude back-ups simuleren (datums ver in het verleden, sorteren voor de echte).
    for i in range(collector.BACKUP_KEEP + 3):
        (backup_dir / f"history_2020-01-{i + 1:02d}.db.gz").write_bytes(b"oud")

    collector.backup_history()

    backups = sorted(backup_dir.glob("history_*.db.gz"))
    assert len(backups) == collector.BACKUP_KEEP
    # De nieuwste (die van vandaag) moet bewaard zijn gebleven.
    assert backups[-1].name == f"history_{date.today().isoformat()}.db.gz"


def test_backup_latest_endpoint(client, temp_db):
    # Nog geen back-up: nette 404.
    resp = client.get("/api/backup/latest")
    assert resp.status_code == 404

    _insert_history(temp_db)
    collector.backup_history()

    resp = client.get("/api/backup/latest")
    assert resp.status_code == 200
    # Moet een geldig gzip-bestand teruggeven.
    assert resp.data[:2] == b"\x1f\x8b"
