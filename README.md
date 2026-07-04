# Busvervoer-monitor provincie Utrecht

Lokale app die real-time het busvervoer in de provincie Utrecht (U-OV / Keolis /
Transdev en overige lijnen die de provincie doorkruisen) monitort: live
voertuigposities, vertragingen, storingsmeldingen en punctualiteitsstatistieken.

## Databron

Open, gratis GTFS-Realtime feeds van NDOV/OVapi (`gtfs.ovapi.nl`) — geen
API-key nodig. Relevante lijnen worden bepaald door de statische GTFS-feed te
filteren op haltes die daadwerkelijk binnen de provinciegrens van Utrecht
liggen (echte polygon, niet een bounding box), niet op operatornaam alleen —
dat vangt automatisch grensoverschrijdende lijnen van andere vervoerders mee.

## Starten

```powershell
./start.ps1
```

Dashboard: http://127.0.0.1:5151

De app verzamelt elke 30 seconden nieuwe data op de achtergrond en slaat die
op in `data/bus_monitor.db` (SQLite). Ruwe metingen worden na 14 dagen
opgerold tot dagstatistieken per lijn, zodat de trendweergave blijft werken
zonder dat de database onbeperkt groeit.

## Statische data verversen

De statische dienstregeling (welke lijnen/haltes er zijn) verandert af en toe.
Ververs de index zo nu en dan (bv. maandelijks, of na een grote
dienstregelingswijziging):

```powershell
./venv/Scripts/python.exe -m app.build_static_index
```

Dit downloadt (indien nog niet aanwezig) de landelijke GTFS-zip
(`data/gtfs-nl.zip`, ~240 MB) en herbouwt `data/utrecht_*.json`. Verwijder
`data/gtfs-nl.zip` handmatig als je een verse download wilt forceren.

## Projectstructuur

- `app/build_static_index.py` — bepaalt welke haltes/routes/agencies binnen
  de provincie Utrecht vallen (eenmalig/periodiek).
- `app/gtfs_rt.py` — haalt en filtert de drie realtime feeds (posities,
  vertragingen, storingen).
- `app/collector.py` — achtergrondscheduler die elke 30s data ophaalt en
  opslaat, plus periodieke opschoning.
- `app/db.py` — SQLite-schema en connectie.
- `app/server.py` — Flask API + dashboard.
- `templates/index.html` — dashboard (kaart, storingen, statistieken).
