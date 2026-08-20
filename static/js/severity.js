/* Ernstbepaling van meldingen -- gedeeld door templates/index.html (het
   volledige dashboard) en templates/lite.html (de publieke versie), net
   zoals static/css/theme.css het thema deelt.

   Stond eerder letterlijk twee keer in beide templates, met een comment
   "bewust in sync houden". Dat ging goed tot een wijziging aan de
   NS-drempel die in beide bestanden identiek doorgevoerd moest worden --
   precies het soort verdubbeling dat vroeg of laat uit elkaar loopt. De
   Python-kant (_is_severe_alert() in app/collector.py, die bepaalt wat er
   in de RSS-feed komt) blijft wel een eigen implementatie: dat is een
   ander proces, en de lijst hieronder moet daar nog steeds handmatig mee
   in de pas blijven.

   Let op: static/ hot-reloadt wel, templates cachen bij debug=False -- na
   een wijziging hier is dus geen herstart nodig, na een wijziging in de
   templates wel. */

// Trefwoorden die een melding als urgent aanmerken. Het GTFS-RT effect-veld
// is voor veel meldingen te generiek, terwijl de vrije tekst vaak wel
// aangeeft dat het om iets ernstigers gaat. "verstoring" bevat toevallig de
// substring "storing", dus die moet eerder gecontroleerd worden.
//
// ADDITIONAL_SERVICE ("Extra dienst") kreeg ooit altijd de ernstig-badge,
// omdat vervoerders dat effect soms voor urgente meldingen gebruiken i.p.v.
// daadwerkelijk extra dienst. In de praktijk is verreweg de meeste
// ADDITIONAL_SERVICE-melding gewoon een routine "halte vervalt i.v.m.
// Werkzaamheden/Optocht" -- geen incident. De écht urgente gevallen (bv.
// "i.v.m. een ongeval") worden al door de keywords hieronder gedekt.
//
// "stremming" is bewust geen keyword: de description van KV15-afkomstige
// meldingen bevat standaard "Oorzaak : Stremming Effect : Omleiding
// Maatregelen : ..." bij ELKE aangekondigde halte-verplaatsing, ook
// routinematige, dagen van tevoren geplande.
const SEVERE_ALERT_KEYWORDS = [
  'verstoring', 'storing', 'brand', 'hulpdiensten', 'politie',
  'ongeval', 'aanrijding', 'ambulance', 'gewonde', 'calamiteit',
];

// GTFS-RT Alert.cause is een betrouwbaarder signaal dan tekst-keywords als de
// vervoerder het daadwerkelijk vult (vaak UNKNOWN_CAUSE, maar als het wel
// gezet is telt het). Alleen de ondubbelzinnig urgente oorzaken;
// MAINTENANCE/CONSTRUCTION/HOLIDAY/WEATHER/TECHNICAL_PROBLEM blijven bewust
// buiten deze lijst -- dat zijn vaak routinemeldingen, geen incidenten.
const SEVERE_CAUSE_LABELS = {
  ACCIDENT: 'Ongeval', POLICE_ACTIVITY: 'Politie', MEDICAL_EMERGENCY: 'Medisch noodgeval',
  DEMONSTRATION: 'Demonstratie', STRIKE: 'Staking',
};

// Hoeveel haltes een melding moet raken voordat een trefwoord 'm urgent
// maakt. Zonder deze ondergrens keek de detectie alleen naar het WOORD en
// nooit naar de OMVANG: een ongeval waardoor twee haltes een paar uur
// vervielen kreeg exact dezelfde rode badge als een ongeval waardoor de
// hele tramlijn tussen Westraven en Jaarbeursplein stillag (4 tegenover 16
// stop_ids). Haltes komen per rijrichting dubbel in de feed voor, dus 6
// stop_ids is ongeveer 3 fysieke haltes.
//
// Toegegeven: dit blijft een gekozen getal, geen wetmatigheid. Een zwaar
// incident dat toevallig maar één halte raakt glipt er dus doorheen. Het is
// wel een betere vraag dan voorheen -- "hoeveel is er geraakt" in plaats van
// alleen "staat het woord er".
const SEVERE_ALERT_MIN_STOPS = 6;

// Een melding die een hele lijn raakt (routes gevuld) telt sowieso als groot
// genoeg: dan heeft de vervoerder de storing op lijnniveau aangemeld, wat
// per definitie breder is dan een paar losse haltes.
function severeAlertIsLargeEnough(a) {
  if (a.routes && a.routes.length) return true;
  return (a.stops || []).length >= SEVERE_ALERT_MIN_STOPS;
}

// Geeft een label ('Ongeval') als de melding urgent is, anders null.
// Nog te beginnen meldingen (valid_from in de toekomst) tellen niet als
// urgent -- dat is dan een aankondiging van gepland werk, geen actuele
// situatie. Zelfde regel als _is_severe_alert() in app/collector.py.
function severeAlertLabel(a) {
  if (a.valid_from && a.valid_from > Date.now() / 1000) return null;
  if (!severeAlertIsLargeEnough(a)) return null;
  const text = `${a.header || ''} ${a.description || ''}`.toLowerCase();
  const kw = SEVERE_ALERT_KEYWORDS.find(k => text.includes(k));
  if (kw) return kw.charAt(0).toUpperCase() + kw.slice(1);
  return SEVERE_CAUSE_LABELS[a.cause] || null;
}

// NS-spoorstoringen: aparte databron (app/ns_rail_alerts.py), al op de
// server gefilterd tot storingen die een station in de provincie Utrecht
// raken. impact.value loopt bij NS van 1 (licht) t/m 3 (trein rijdt niet).
//
// MAINTENANCE (geplande werkzaamheden) telt bewust NOOIT als ernstig, ook
// niet bij hoge impact: vervangend busvervoer bij groot gepland werk scoort
// bij NS net zo'n hoog impact-getal als een echte "trein rijdt niet"-
// verstoring, puur op basis van hoeveel dienst wegvalt -- niet op basis van
// gepland/ongepland.
//
// Drempel voor DISRUPTION/CALAMITY staat op impact>=2 (niet >=3): een echte
// verstoring met "minder treinen"/"veel minder treinen" (impact 2) is nog
// steeds een acuut incident, ook al rijden er niet nul treinen.
function railAlertSeverity(a) {
  if (a.disruption_type === 'MAINTENANCE') return false;
  return a.disruption_type === 'CALAMITY' || (a.impact !== null && a.impact >= 2);
}
