# Busvervoer-monitor U-OV (provincie Utrecht)

Lokale app die real-time het busvervoer van U-OV monitort — de concessie voor
busvervoer in de provincie Utrecht, uitgevoerd door Keolis en Transdev onder
de gezamenlijke merknaam U-OV: live voertuigposities, vertragingen,
storingsmeldingen en punctualiteitsstatistieken.

## Databron

Open, gratis GTFS-Realtime feeds van NDOV/OVapi (`gtfs.ovapi.nl`) — geen
API-key nodig. Relevante lijnen worden bepaald door de statische GTFS-feed te
filteren op `agency_id == "UOV"` (bus, geen sneltram) — dus precies de
officiële U-OV-concessie, geen andere vervoerders die toevallig de provincie
doorkruisen (Qbuzz, Connexxion, Arriva, GVB, NS-bus e.d. worden bewust
uitgesloten).

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

- `app/build_static_index.py` — filtert de landelijke GTFS-feed op de U-OV-
  concessie (agency_id UOV, bus) en de bijbehorende trips/haltes (eenmalig/periodiek).
- `app/gtfs_rt.py` — haalt en filtert de drie realtime feeds (posities,
  vertragingen, storingen).
- `app/collector.py` — achtergrondscheduler die elke 30s data ophaalt en
  opslaat, plus periodieke opschoning.
- `app/db.py` — SQLite-schema en connectie.
- `app/server.py` — Flask API + dashboard.
- `templates/index.html` — dashboard (kaart, storingen, statistieken).
