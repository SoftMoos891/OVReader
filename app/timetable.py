"""Dienstregeling-opzoek: haltezoeker en eerstvolgende vertrekken, op basis
van de door build_static_index.py gegenereerde statische bestanden
(utrecht_stop_times.json, utrecht_calendar.json, utrecht_trip_meta.json),
verrijkt met live vertraging uit de realtime-databank (app/db.py)."""
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

WEEKDAY_FIELDS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
LIVE_DELAY_FRESHNESS_SECONDS = 20 * 60  # hoe oud een laatst-bekende vertraging nog mag zijn

# Herkent een perron-/kant-aanduiding aan het eind van een haltenaam, bv.
# "(C1)", "(A)", "(D12)" -- 1-2 letters plus optioneel een kort getal. Bewust
# NIET woorden als "(Oost)"/"(laag)"/"(Tak)" (die zijn 3+ letters en horen
# dus niet bij dit patroon) -- dat zijn losse, betekenisvol verschillende
# locaties, geen perrons van dezelfde overstaphalte. Geverifieerd tegen de
# volledige utrecht_stops.json: 62 namen matchen (allemaal echte
# perroncodes van 9 verschillende stations/busstations), de 8 die niet
# matchen zijn stuk voor stuk echte woorden (Oost/West/noord/zuid/hoog/
# laag/Tak/Zuidzijde).
_PLATFORM_SUFFIX_RE = re.compile(r"\s*\([A-Za-z]{1,2}\d{0,3}\)$")


def _stop_group_name(name):
    """Basisnaam van een halte zonder perron-/kant-suffix, voor het
    groeperen van haltes die feitelijk dezelfde overstaplocatie zijn."""
    return _PLATFORM_SUFFIX_RE.sub("", name)


def _platform_label(stop):
    """Perronaanduiding van één halte-paal, of None.

    Neemt het achtervoegsel uit de haltenaam als dat er is, en pas anders
    het officiele platform_code-veld. Die volgorde is bewust: bij vier palen
    van Utrecht Station Overvecht spreken de twee bronnen elkaar tegen (naam
    zegt "(A)" waar platform_code "B" zegt, en omgekeerd -- ze staan er
    paarsgewijs omgedraaid in). De naam is wat er op het bord bij de halte
    staat en wat de reiziger elders in de app ziet, dus die wint; buiten dat
    ene station zijn ze het overal eens.

    platform_code levert daarnaast juist de gevallen op die de naam mist:
    de perrons van bv. Utrecht CS Jaarbeurszijde heten allemaal exact
    hetzelfde en zijn alleen via dit veld uit elkaar te houden."""
    name = stop.get("name", "")
    match = _PLATFORM_SUFFIX_RE.search(name)
    if match:
        return match.group().strip()[1:-1]  # "(C1)" -> "C1"
    return stop.get("platform_code") or None


class Timetable:
    def __init__(self):
        self.stops = {}          # stop_id -> {name, lat, lon}
        self.stop_times = {}     # stop_id -> [(trip_id, stop_sequence, time_str), ...]
        self.trip_meta = {}      # trip_id -> {route_id, service_id, headsign}
        self.calendar = {}       # service_id -> {days, start_date, end_date, added, removed}
        self.loaded_at = 0
        self.reload()

    def reload(self):
        stops_path = DATA_DIR / "utrecht_stops.json"
        stop_times_path = DATA_DIR / "utrecht_stop_times.json"
        trip_meta_path = DATA_DIR / "utrecht_trip_meta.json"
        calendar_path = DATA_DIR / "utrecht_calendar.json"

        # utrecht_stops.json bestaat al veel langer (gebruikt door UtrechtIndex)
        # -- laad 'm onafhankelijk zodat de haltezoeker op naam altijd werkt,
        # ook als de nieuwere dienstregelingbestanden hieronder nog ontbreken.
        self.stops = json.loads(stops_path.read_text(encoding="utf-8")) if stops_path.exists() else {}
        # Los van stop_times.json opbouwen (hangt alleen van self.stops af),
        # anders bestaat dit attribuut niet als stop_times.json nog ontbreekt.
        self._build_stop_groups()

        if not stop_times_path.exists():
            # Bestaande installaties hebben deze bestanden pas na een herbouw
            # van de statische index (build_static_index.py) -- eerstvolgende
            # vertrekken staan tot die tijd uit i.p.v. de hele app te laten crashen.
            print(
                "[timetable] utrecht_stop_times.json ontbreekt -- eerstvolgende "
                "vertrekken staan uit totdat app/build_static_index.py opnieuw is "
                "gedraaid (haltezoeken op naam werkt al wel)."
            )
            self.stop_times, self.trip_meta, self.calendar = {}, {}, {}
            self.loaded_at = time.time()
            return
        self.stop_times = json.loads(stop_times_path.read_text(encoding="utf-8"))
        self.trip_meta = json.loads(trip_meta_path.read_text(encoding="utf-8"))
        self.calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
        self.loaded_at = time.time()

    def _build_stop_groups(self):
        """group_name (zie _stop_group_name) -> lijst van alle stop_id's die
        tot die overstaphalte horen. Gebruikt door next_departures() om de
        vertrekken van alle perrons samen te tellen -- search_stops() toont
        maar één representatief perron per groep, maar de vertrekken moeten
        wel van alle perrons samen komen, anders mis je lijnen die toevallig
        niet van het drukste/gekozen perron vertrekken."""
        self.stop_group_members = {}
        for stop_id, s in self.stops.items():
            group = _stop_group_name(s.get("name", ""))
            self.stop_group_members.setdefault(group, []).append(stop_id)

    def search_stops(self, query, limit=25):
        """Zoekt haltes op (deel van de) naam, case-insensitive. Haltes waarvan
        de naam met de zoekterm begint staan bovenaan."""
        q = (query or "").strip().lower()
        if not q:
            return []
        starts, contains = [], []
        for stop_id, s in self.stops.items():
            name = s.get("name", "")
            name_lower = name.lower()
            if name_lower.startswith(q):
                starts.append((stop_id, s))
            elif q in name_lower:
                contains.append((stop_id, s))
        starts.sort(key=lambda x: x[1].get("name", ""))
        contains.sort(key=lambda x: x[1].get("name", ""))
        ordered = starts + contains

        # Meerdere stop_id's zijn vaak feitelijk dezelfde overstaphalte --
        # losse perrons met exact dezelfde naam (bv. "Utrecht, CS
        # Jaarbeurszijde" voor C1 t/m C10) of met de perronletter/-kant in de
        # naam zelf (bv. "Utrecht, CS Jaarbeurszijde (C1)", "(C2)", ...) --
        # zie _stop_group_name(). Zonder dedup leverde dat een zoekresultaat
        # vol ogenschijnlijk identieke haltes op. Per groep blijft alleen het
        # perron met de meeste geplande vertrekken over (grootste kans op
        # een bruikbaar vertrekkenoverzicht) -- de rest van de perrons
        # blijft gewoon bestaan, ze worden alleen niet los in de zoeklijst
        # getoond, en het vertrekkenoverzicht toont daarna gewoon de precieze
        # naam (incl. perronletter) van het gekozen perron. Een dict onthoudt
        # insertievolgorde, dus starts-with-eerst blijft ook na het
        # vervangen door een drukker perron behouden.
        by_group = {}
        for stop_id, s in ordered:
            name = s.get("name", "")
            group = _stop_group_name(name)
            current = by_group.get(group)
            if current is None or len(self.stop_times.get(stop_id, [])) > len(self.stop_times.get(current[0], [])):
                by_group[group] = (stop_id, s)
        results = list(by_group.values())[:limit]
        return [{"stop_id": sid, "name": _stop_group_name(s.get("name", "")), "lat": s.get("lat"), "lon": s.get("lon")}
                for sid, s in results]

    def active_service_ids(self, target_date: date):
        """Geeft de service_ids terug die op de opgegeven datum rijden,
        rekening houdend met calendar.txt (weekdag + geldigheidsperiode) en
        calendar_dates.txt-uitzonderingen (toegevoegd/geschrapt)."""
        date_str = target_date.strftime("%Y%m%d")
        weekday = target_date.weekday()  # maandag=0 .. zondag=6, komt overeen met WEEKDAY_FIELDS
        active = set()
        for service_id, entry in self.calendar.items():
            start, end = entry.get("start_date", ""), entry.get("end_date", "")
            in_range = bool(start) and bool(end) and start <= date_str <= end
            scheduled = in_range and bool(entry.get("days", [False] * 7)[weekday])
            if date_str in entry.get("removed", []):
                scheduled = False
            if date_str in entry.get("added", []):
                scheduled = True
            if scheduled:
                active.add(service_id)
        return active

    def _live_delay_by_trip(self, trip_ids, stop_id, now_ts):
        """Laatst bekende vertraging (seconden) per trip_id voor deze halte,
        binnen het freshness-venster."""
        if not trip_ids:
            return {}
        cutoff = now_ts - LIVE_DELAY_FRESHNESS_SECONDS
        placeholders = ",".join("?" * len(trip_ids))
        conn = db.get_conn()
        try:
            rows = conn.execute(
                f"""
                SELECT trip_id, arrival_delay, departure_delay
                FROM trip_delays
                WHERE stop_id = ? AND trip_id IN ({placeholders}) AND fetched_at >= ?
                GROUP BY trip_id
                HAVING fetched_at = MAX(fetched_at)
                """,
                [stop_id, *trip_ids, cutoff],
            ).fetchall()
        finally:
            conn.close()
        return {
            r["trip_id"]: r["arrival_delay"] if r["arrival_delay"] is not None else r["departure_delay"]
            for r in rows
        }

    def next_departures(self, stop_id, now_ts, window_minutes=90, limit=30):
        """Eerstvolgende vertrekken voor stop_id, geaggregeerd over alle
        perrons van dezelfde overstaphalte (zie _build_stop_groups()) --
        niet alleen het ene perron dat search_stops() als representant
        koos. Zonder dit mistte je lijnen die toevallig niet vanaf dat ene
        gekozen perron vertrekken, terwijl je op de gegroepeerde naam had
        gezocht en dus de hele overstaphalte verwachtte."""
        group = _stop_group_name(self.stops.get(stop_id, {}).get("name", ""))
        member_ids = self.stop_group_members.get(group) or [stop_id]

        now_dt = datetime.fromtimestamp(now_ts)
        today = now_dt.date()
        candidates = []
        for day_offset in (-1, 0):
            d = today + timedelta(days=day_offset)
            active = self.active_service_ids(d)
            if not active:
                continue
            midnight = datetime.combine(d, datetime.min.time())
            for member_id in member_ids:
                for entry in self.stop_times.get(member_id, []):
                    # Normaal 3 elementen; een vierde (waarde 2) betekent
                    # "alleen op afroep" -- zie find_stop_times_for_trips()
                    # in build_static_index.py. Vertrekken waar je helemaal
                    # niet kunt instappen (pickup_type 1, de eindhalte van
                    # vrijwel elke rit) staan daar al niet meer in.
                    trip_id, _stop_sequence, time_str = entry[0], entry[1], entry[2]
                    on_demand = len(entry) > 3 and entry[3] == 2
                    meta = self.trip_meta.get(trip_id)
                    if not meta or meta.get("service_id") not in active:
                        continue
                    seconds = _parse_gtfs_time(time_str)
                    if seconds is None:
                        continue
                    scheduled_dt = midnight + timedelta(seconds=seconds)
                    candidates.append((scheduled_dt, trip_id, meta, member_id, on_demand))

        window_start = now_dt - timedelta(minutes=1)  # kleine marge voor net-vertrokken bussen
        window_end = now_dt + timedelta(minutes=window_minutes)
        upcoming = [c for c in candidates if window_start <= c[0] <= window_end]
        upcoming.sort(key=lambda c: c[0])

        # Bij een grote overstaphalte (veel perrons samen, bv. Utrecht CS
        # Jaarbeurszijde: 270+ vertrekken binnen 90 min) zou puur
        # chronologisch afkappen op limit de latere lijnen helemaal
        # wegcijferen, ook al vertrekken ze wel degelijk binnen het venster
        # -- exact het "ik mis nu lijnen"-probleem. Eerst de vroegste rit
        # per lijn (line-diversiteit), dan de rest chronologisch aanvullen
        # tot de limiet, en pas daarna weer op tijd sorteren voor weergave.
        seen_routes = set()
        priority, rest = [], []
        for c in upcoming:
            route_id = c[2].get("route_id")
            if route_id not in seen_routes:
                seen_routes.add(route_id)
                priority.append(c)
            else:
                rest.append(c)
        upcoming = (priority + rest)[:limit]
        upcoming.sort(key=lambda c: c[0])

        # Live vertraging is per (trip_id, stop_id) -- een trip stopt maar bij
        # één specifiek perron, dus _live_delay_by_trip() blijft ongewijzigd
        # (kent alleen een enkel stop_id), maar wordt nu per perron dat
        # daadwerkelijk in upcoming voorkomt apart aangeroepen i.p.v. één
        # keer voor het hele stop_id.
        trip_ids_by_member = {}
        for _dt, trip_id, _meta, member_id, _on_demand in upcoming:
            trip_ids_by_member.setdefault(member_id, []).append(trip_id)
        delay_by_trip_member = {}
        for member_id, trip_ids in trip_ids_by_member.items():
            for trip_id, delay in self._live_delay_by_trip(trip_ids, member_id, now_ts).items():
                delay_by_trip_member[(trip_id, member_id)] = delay

        # Sommige haltes hebben 2 stop_id's zonder eigen perronaanduiding
        # (bv. één paal per rijrichting) -- dan voegt een "perron"-kolom
        # niets toe. Alleen tonen als er echt meerdere, van elkaar te
        # onderscheiden perrons zijn. Dit keek eerder naar de halteNAMEN,
        # waardoor het misging bij haltes waar alle perrons identiek heten
        # (Utrecht CS Jaarbeurszijde: zeven perrons, één naam) -- daar bleef
        # de kolom weg terwijl die juist daar het meest nodig is.
        platform_by_member = {
            m: _platform_label(self.stops.get(m, {})) for m in member_ids
        }
        multi_platform = len({p for p in platform_by_member.values() if p}) > 1
        results = []
        for scheduled_dt, trip_id, meta, member_id, on_demand in upcoming:
            delay = delay_by_trip_member.get((trip_id, member_id))
            estimated_dt = scheduled_dt + timedelta(seconds=delay) if delay is not None else None
            results.append({
                "trip_id": trip_id,
                "route_id": meta.get("route_id"),
                "headsign": meta.get("headsign", ""),
                "scheduled_time": scheduled_dt.strftime("%H:%M"),
                "estimated_time": estimated_dt.strftime("%H:%M") if estimated_dt else None,
                "delay_seconds": delay,
                "is_live": delay is not None,
                # U-Flex e.d.: rijdt alleen als je 'm vooraf reserveert, dus
                # geen vertrek waar je zomaar op kunt gaan staan wachten.
                "on_demand": on_demand,
                # Alleen ingevuld als de halte meerdere perrons heeft (anders
                # overbodige info) -- laat zien vanaf welk exact perron een
                # vertrek gaat, nu er meerdere door elkaar heen getoond worden.
                # Al kant-en-klaar als "C1"/"A", dus de frontend hoeft er geen
                # perroncode meer uit een haltenaam te vissen.
                "platform": platform_by_member.get(member_id) if multi_platform else None,
            })
        return results


def _parse_gtfs_time(time_str):
    """Parseert een GTFS-tijd ('HH:MM:SS', mag >=24:00:00 zijn voor diensten
    die na middernacht doorlopen) naar seconden sinds middernacht."""
    if not time_str:
        return None
    parts = time_str.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s
