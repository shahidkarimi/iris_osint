"""Tiny i18n layer — German is the primary UI language, English is a toggle."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

LOCALES = ("de", "en")
BERLIN = ZoneInfo("Europe/Berlin")

MESSAGES: dict[str, dict[str, str]] = {
    # navigation
    "nav.dashboard": {"de": "Lagebild", "en": "Overview"},
    "nav.signals": {"de": "Signale", "en": "Signals"},
    "nav.map": {"de": "Karte", "en": "Map"},
    "nav.quality": {"de": "Datenqualität", "en": "Data quality"},
    "nav.method": {"de": "Methodik", "en": "Methodology"},
    "app.tagline": {"de": "Signal-Harvester · JLL Deutschland", "en": "Signal harvester · JLL Germany"},
    # signal types
    "type.commercial_land_disposal": {"de": "Gewerbegrundstück-Transaktion", "en": "Commercial land disposal"},
    "type.big_box_industrial_development": {
        "de": "Big-Box Industrie & Logistik",
        "en": "Big-box industrial development",
    },
    "type.short.commercial_land_disposal": {"de": "Grundstück", "en": "Land"},
    "type.short.big_box_industrial_development": {"de": "Big-Box", "en": "Big-box"},
    "type.all": {"de": "Alle Signaltypen", "en": "All signal types"},
    # geocode
    "geo.resolved": {"de": "verortet", "en": "resolved"},
    "geo.ambiguous": {"de": "mehrdeutig", "en": "ambiguous"},
    "geo.unresolved": {"de": "nicht verortet", "en": "unresolved"},
    "geo.invalid": {"de": "ungültig verworfen", "en": "rejected as invalid"},
    "geo.all": {"de": "Alle Verortungen", "en": "All locations"},
    "prec.address": {"de": "Adresse", "en": "Address"},
    "prec.street": {"de": "Straße", "en": "Street"},
    "prec.district": {"de": "Stadtteil", "en": "District"},
    "prec.municipality": {"de": "Gemeinde", "en": "Municipality"},
    "prec.—": {"de": "ohne Punkt", "en": "no point"},
    # deal types
    "deal.acquisition": {"de": "Erwerb", "en": "Acquisition"},
    "deal.disposal": {"de": "Verkauf", "en": "Disposal"},
    "deal.letting": {"de": "Anmietung", "en": "Letting"},
    "deal.development": {"de": "Projektentwicklung", "en": "Development"},
    "deal.unknown": {"de": "unbestimmt", "en": "unspecified"},
    # dashboard
    "kpi.signals": {"de": "Signale im Staging", "en": "Signals in staging"},
    "kpi.land": {"de": "Grundstücks­transaktionen", "en": "Land disposals"},
    "kpi.bigbox": {"de": "Big-Box-Vorhaben", "en": "Big-box schemes"},
    "kpi.geocoded": {"de": "mit EPSG:25832-Punkt", "en": "with EPSG:25832 point"},
    "kpi.precise": {"de": "adress­genau", "en": "address-level"},
    "kpi.plot_area": {"de": "Grundstücksfläche gesamt", "en": "Total plot area"},
    "kpi.bigbox_area": {"de": "Hallen-/Nutzfläche Big-Box", "en": "Big-box floor area"},
    "kpi.municipalities": {"de": "Gemeinden", "en": "municipalities"},
    "dash.timeline": {"de": "Signale pro Quartal", "en": "Signals per quarter"},
    "dash.latest": {"de": "Neueste Signale", "en": "Latest signals"},
    "dash.towns": {"de": "Top-Standorte", "en": "Top locations"},
    "dash.map": {"de": "Räumliche Verteilung", "en": "Spatial distribution"},
    "dash.lastrun": {"de": "Letzter Lauf", "en": "Last run"},
    "dash.lastrun_ok": {"de": "Letzter erfolgreicher Lauf", "en": "Last successful run"},
    "dash.last_attempt": {"de": "Letzter Versuch", "en": "Latest attempt"},
    "dash.pipeline": {"de": "Pipeline", "en": "Pipeline"},
    "dash.open_map": {"de": "Karte öffnen", "en": "Open map"},
    "dash.all_signals": {"de": "Alle Signale", "en": "All signals"},
    "dash.corpus": {"de": "Korpus-Entscheidungen", "en": "Corpus decisions"},
    "dash.precision": {"de": "Verortungsgenauigkeit", "en": "Location precision"},
    # signals
    "sig.search": {"de": "Titel, Ort, Volltext …", "en": "Title, place, full text …"},
    "sig.filter": {"de": "Filtern", "en": "Filter"},
    "sig.reset": {"de": "Zurücksetzen", "en": "Reset"},
    "sig.results": {"de": "Treffer", "en": "results"},
    "sig.year": {"de": "Jahr", "en": "Year"},
    "sig.all_years": {"de": "Alle Jahre", "en": "All years"},
    "sig.sort": {"de": "Sortierung", "en": "Sort"},
    "sort.published_desc": {"de": "Neueste zuerst", "en": "Newest first"},
    "sort.published_asc": {"de": "Älteste zuerst", "en": "Oldest first"},
    "sort.confidence_desc": {"de": "Konfidenz", "en": "Confidence"},
    "sort.area_desc": {"de": "Fläche", "en": "Area"},
    "sig.none": {"de": "Keine Signale für diese Filter.", "en": "No signals match these filters."},
    "sig.published": {"de": "Veröffentlicht", "en": "Published"},
    "sig.location": {"de": "Standort", "en": "Location"},
    "sig.area": {"de": "Fläche", "en": "Area"},
    "sig.plot": {"de": "Grundstück", "en": "Plot"},
    "sig.floor": {"de": "Halle/Nutzfl.", "en": "Floor area"},
    "sig.confidence": {"de": "Konfidenz", "en": "Confidence"},
    "sig.source": {"de": "Quelle", "en": "Source"},
    "sig.prev": {"de": "Zurück", "en": "Previous"},
    "sig.next": {"de": "Weiter", "en": "Next"},
    "sig.download": {"de": "Export", "en": "Export"},
    # detail
    "det.back": {"de": "Alle Signale", "en": "All signals"},
    "det.facts": {"de": "Eckdaten", "en": "Key facts"},
    "det.deal": {"de": "Transaktionsart", "en": "Deal type"},
    "det.open_source": {"de": "Pressemitteilung öffnen", "en": "Open press release"},
    "det.location": {"de": "Verortung", "en": "Location resolution"},
    "det.status": {"de": "Status", "en": "Status"},
    "det.precision": {"de": "Genauigkeit", "en": "Precision"},
    "det.provider": {"de": "Geocoder", "en": "Geocoder"},
    "det.coords": {"de": "Koordinaten", "en": "Coordinates"},
    "det.candidates": {"de": "Erkannte Ortsangaben", "en": "Detected place mentions"},
    "det.notes": {"de": "Hinweise", "en": "Notes"},
    "det.classification": {"de": "Klassifikation", "en": "Classification"},
    "det.rules": {"de": "Ausgelöste Regeln", "en": "Rules fired"},
    "det.evidence": {"de": "Belegstelle", "en": "Evidence"},
    "det.lineage": {"de": "Herkunft & Versionierung", "en": "Lineage & versioning"},
    "det.flags": {"de": "Qualitätshinweise", "en": "Quality flags"},
    "det.lead": {"de": "Auszug", "en": "Excerpt"},
    "det.nearby": {"de": "Signale im Umkreis von 50 km", "en": "Signals within 50 km"},
    "det.raw": {"de": "Staging-Datensatz (JSON)", "en": "Staging record (JSON)"},
    "det.no_point": {"de": "Kein Punkt gespeichert", "en": "No point stored"},
    "det.no_point_text": {
        "de": "Die Quelle lässt keinen eindeutigen Standort zu. Statt einer geratenen Koordinate werden die Kandidaten dokumentiert.",
        "en": "The source does not allow a single unambiguous site. Instead of guessing a coordinate, the candidates are documented.",
    },
    # quality
    "q.checks": {"de": "Validierungsabfragen", "en": "Validation queries"},
    "q.runs": {"de": "Harvest-Läufe", "en": "Harvest runs"},
    "q.issues": {"de": "Fehler & Warnungen (letzter Lauf)", "en": "Errors & warnings (latest run)"},
    "q.source": {"de": "Quellenprüfung", "en": "Source verification"},
    "q.no_point": {"de": "Datensätze ohne Punkt", "en": "Records without a point"},
    "q.pass": {"de": "bestanden", "en": "passed"},
    "q.fail": {"de": "fehlgeschlagen", "en": "failed"},
    "q.rejections": {"de": "Ausgeschlossene Dokumente", "en": "Rejected documents"},
    "q.report": {"de": "Qualitätsbericht (Markdown)", "en": "Quality report (Markdown)"},
    "q.all_clear": {"de": "Keine Fehler oder Warnungen.", "en": "No errors or warnings."},
    # runs
    "run.status.succeeded": {"de": "erfolgreich", "en": "succeeded"},
    "run.status.partial": {"de": "teilweise", "en": "partial"},
    "run.status.failed": {"de": "fehlgeschlagen", "en": "failed"},
    "run.status.running": {"de": "läuft", "en": "running"},
    "empty.title": {"de": "Noch keine Daten", "en": "No data yet"},
    "empty.text": {
        "de": "Führen Sie einen Harvest aus, um das Staging zu befüllen:",
        "en": "Run a harvest to populate staging:",
    },
    "footer.data": {
        "de": "Daten: öffentliche Pressemitteilungen von JLL Deutschland · Ortsdaten © GeoNames (CC BY 4.0), © OpenStreetMap-Mitwirkende",
        "en": "Data: public JLL Germany press releases · Places © GeoNames (CC BY 4.0), © OpenStreetMap contributors",
    },
}


def translate(key: str, locale: str) -> str:
    entry = MESSAGES.get(key)
    if entry is None:
        return key.rsplit(".", 1)[-1]
    return entry.get(locale) or entry.get("de") or key


def fmt_number(value: float | int | Decimal | None, locale: str, digits: int = 0) -> str:
    if value is None:
        return "—"
    text = f"{float(value):,.{digits}f}"
    if locale == "de":
        text = text.replace(",", " ").replace(".", ",")
    return text


def fmt_area(value: float | Decimal | None, locale: str, *, hectares: bool = True) -> str:
    """Plots read naturally in ha; floor areas always in m² (``hectares=False``)."""
    if value is None:
        return "—"
    v = float(value)
    if hectares and v >= 100_000:
        return f"{fmt_number(v / 10_000, locale, 1)} ha"
    return f"{fmt_number(v, locale)} m²"


def fmt_date(value: datetime | None, locale: str, with_time: bool = False) -> str:
    if value is None:
        return "—"
    local = value.astimezone(BERLIN) if value.tzinfo else value
    if locale == "de":
        return local.strftime("%d.%m.%Y, %H:%M") if with_time else local.strftime("%d.%m.%Y")
    return local.strftime("%Y-%m-%d %H:%M") if with_time else local.strftime("%Y-%m-%d")
