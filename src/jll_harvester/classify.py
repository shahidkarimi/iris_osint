"""Rule-based signal classification (German press-release language + JLL taxonomy).

Two signal types are in scope (objective of IRIS-SIG-45):

``commercial_land_disposal``
    A plot of land / site / brownfield (not a standing building) changes hands — bought, sold,
    disposed — for commercial, industrial or mixed use. Purely residential land is excluded.

``big_box_industrial_development``
    An industrial / logistics / production / data-centre scheme that is being developed
    (Projektentwicklung, Neubau, Baustart, Fertigstellung, spekulativ …), **or** an industrial
    transaction/letting of a big-box asset (≥ ``big_box_min_sqm``, default 10 000 m²).

Out of scope and rejected with a reason: personnel news, aggregate market research
(e.g. "Logistikinvestmentmarkt bestätigt Aufwärtstrend"), office/retail/residential-only deals.

Every decision carries an ``evidence`` dict (matched rules + snippets) so reviewers can audit it.
The rules are deliberately explicit rather than ML-based: the corpus is small (≈700 pages),
precision matters more than recall for a staging feed, and explainability is a requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Article, Classification, SignalType

# ---------------------------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------------------------
_LAND_RE = re.compile(
    r"\b(?:grundstücks?(?:fläche|teil)?(?:e|en|s)?|\w*grundstück\w*|areal(?:s|e|en)?|\w*gelände(?:s)?|"
    r"brownfield\w*|greenfield\w*|bauland\w*|entwicklungsfläche\w*|konversionsfläche\w*|"
    r"baufeld(?:er|s)?|gewerbefläche(?:n)?\b(?=.{0,40}(?:verkauf|erw[oi]rb|kauf)))",
    re.I,
)
_LAND_TITLE_RE = re.compile(r"grundstück|areal|gelände|brownfield|baufeld|bauland|konversionsfläche", re.I)

_TRANSACTION_RE = re.compile(
    r"\b(?:erw[oi]rb\w*|erworben|kauf(?:t|en|te|vertrag)?|gekauft|ankauf\w*|verkauf\w*|"
    r"veräußer\w*|übernimmt|übernommen|übernahme|sichert\s+sich|"
    r"transaktion\w*|käufer\w*|verkäufer\w*)",
    re.I,
)
_DISPOSAL_RE = re.compile(r"\b(?:verkauf\w*|veräußer\w*|verkäufer\w*|abgegeben|trennt\s+sich)", re.I)
_ACQUISITION_RE = re.compile(
    r"\b(?:erw[oi]rb\w*|erworben|kauf(?:t|en|te)|gekauft|ankauf\w*|übernimmt|sichert\s+sich)", re.I
)
_LETTING_RE = re.compile(
    r"\b(?:miet(?:et|ete|en)|angemietet|anmietung\w*|mietvertrag\w*|vermiet\w*|pre-let|zieht\s+(?:in|ins|nach)|bezieht|verlänger\w*)\b",
    re.I,
)

# Deliberately strict: "Industrieversicherer", "Diakonisches Werk" or a "Halle" must not count.
_INDUSTRIAL_RE = re.compile(
    r"\b(?:\w*logistik\w*|logistics|lager(?:halle|fläche|haus|immobilie|zentrum|gebäude|standort)\w*|"
    r"\w*hallenfläche\w*|\w*halle(?:n)?fläche|distributionszentr\w*|verteilzentr\w*|fulfillment\w*|"
    r"cross-?dock\w*|industrie(?:immobilie|park|fläche|gebiet|halle|anlage|areal|standort|gelände|"
    r"grundstück|bau|objekt|- und)\w*|industrial\w*|light[- ]industrial|gewerbepark\w*|"
    r"produktion(?:s(?:halle|stätte|standort|fläche|gebäude|werk))?\w*|fertigung\w*|fabrik\w*|"
    r"\w*fabrik\w*|werks(?:halle|gelände)\w*|(?:produktions|möbel|automobil|chemie|stahl|zement|motoren)werk\w*|rechenzentr\w*|"
    r"data[- ]?cent(?:er|re)s?|unternehmensimmobilie\w*|verteilzentrum|versandlager\w*)",
    re.I,
)
_DEVELOPMENT_RE = re.compile(
    r"(?:\bprojektentwicklung\w*|\bentwickel(?:t|n)\b|\bentwickl(?:ung|er)\b|\bneubau(?:projekt|vorhaben|s)?\b|"
    r"\berrichten\b|(?:wird|werden|soll|sollen|wurde)\s+(?:\S+\s+){0,5}?(?:errichtet|gebaut|realisiert)|"
    r"\bentsteh(?:t|en)\b|\bbau(?:t|en)\b|\bbaustart\b|\bbaubeginn\b|\bspatenstich\b|\bgrundsteinlegung\b|"
    r"\brichtfest\b|\bfertigstellung\b|\bspekulativ\w*|\bbuil[dt]-to-suit\b|\brevitalisier\w*|"
    r"\bredevelopment\b|\bbaugenehmigung\w*|\bbaurecht\w*|\bbebauungsplan\w*|\bansiedlung\w*|"
    r"\berweiterungsbau\w*|\bbauabschnitt\w*)",
    re.I,
)
_RESEARCH_RE = re.compile(
    r"\b(?:\w*investmentmarkt\w*|\w*vermietungsmarkt\w*|\w*flächenumsatz\w*|\w*spitzenmiete\w*|"
    r"transaktionsvolumen\w*|\w*marktbericht\w*|\w*marktreport\w*|quartal\w*|halbjahr\w*|jahresbilanz|"
    r"marktentwicklung\w*|umfrage\w*|studie\w*|prognose\w*|jahresauftakt|"
    r"angebotsmieten|leerstand\w*|\w*markt\b)",
    re.I,
)
_BUILDING_OBJECT_RE = re.compile(
    r"\b(?:\w*bürogebäude\w*|\w*büroimmobilie\w*|\w*bürohaus\w*|\w*geschäftshaus\w*|wohnanlage\w*|"
    r"\w*einkaufszentrum\w*|\w*fachmarktzentrum\w*|\w*shopping[- ]center\w*|\w*einkaufscenter\w*|"
    r"\w*hotel\w*|\w*pflegeheim\w*|\w*ärztehaus\w*|tiefgarage\w*|baumarkt\w*|\w*portfolio\w*|"
    r"\w*objekt\w*|\w*gebäude\w*|\w*immobilie\w*|\w*zentrale|windkanal\w*|\w*park\b|\w*sitz\b|"
    r"\w*fabrik\w*|\w*werk\b|\w*zentrum\b|\w*halle\w*|\w*hochhaus\w*|\w*komplex\w*)",
    re.I,
)
_STANDING_BUILDING_RE = re.compile(
    r"(?:grundstück\w*|areal|gelände)\W+(?:\w+\W+){0,4}?(?:steht|stehen|befindet|befinden|bebaut|umfasst)|"
    r"(?:gebäude|halle|objekt|immobilie)\w*\W+(?:\w+\W+){0,6}?auf\s+(?:einem|dem)\s+(?:\S+\s+){0,4}?grundstück",
    re.I,
)
_VACANT_RE = re.compile(
    r"\bunbebaut\w*|\bbrachfläche\w*|\bbrownfield|\bgreenfield|\bbaureif\w*|\bbauland|\bkonversion", re.I
)
_RESIDENTIAL_RE = re.compile(
    r"\b(?:wohn(?:ungen|ungsbau\w*|bau\w*|quartier\w*|projekt\w*|anlage\w*|einheiten|raum)|"
    r"mehrfamilienh\w*|einfamilienh\w*|reihenh\w*|eigentumswohnung\w*|micro-?apartment\w*|"
    r"studentenwohn\w*|pbsa)",
    re.I,
)
_COMMERCIAL_USE_RE = re.compile(
    r"\b(?:gewerb\w*|logistik\w*|industrie\w*|büro\w*|unternehmenszentrale|firmenzentrale|"
    r"hauptsitz|produktion\w*|einzelhandel\w*|hotel\w*|rechenzentr\w*|mixed[- ]use|"
    r"gemischt\w*|campus|firmensitz)",
    re.I,
)

# Number formats used by JLL DE: "37.350 m²", "rund 41.200m²", "50 000 m2", "3,5 ha", "11.000 qm"
_AREA_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[.\s  ]\d{3})+|\d+(?:,\d+)?)\s*"
    r"(?P<unit>m²|m2|qm|quadratmeter(?:n)?|ha\b|hektar)",
    re.I,
)
_PLOT_WORDS = re.compile(r"grundstück\w*|areal\w*|gelände\w*|brownfield|baufeld\w*|hektar|\bha\b", re.I)
_BUILDING_WORDS = re.compile(
    r"\w*halle\w*|\w*lager\w*|\w*logistik\w*|\w*miet\w*|\w*nutz\w*|produktion\w*|brutto\w*|bgf|"
    r"gebäude\w*|neubau\w*|büro\w*|\w*fläche\w*|\w*objekt\w*|\w*immobilie\w*",
    re.I,
)
_LETTING_AREA_RE = re.compile(
    r"(?:miet\w*|angemietet|bezieht|bezog|übernimmt|anmietung)\D{0,40}?"
    r"(?P<num>\d{1,3}(?:[.\s\u202f\u00a0]\d{3})+|\d+(?:,\d+)?)\s*(?:m²|m2|qm|quadratmeter)",
    re.I,
)
_DEAL_VERB_RE = re.compile(
    r"\b(?:erw[oi]rb\w*|erworben|kauf(?:t|en)|gekauft|verkauf(?:t|en)|veräußer\w*|übernimmt|"
    r"miet(?:et|en)|angemietet|bezieht|beziehen|zieh(?:t|en)\s+(?:in|ins|nach|auf)|verlager\w*|sichert\s+sich|vermittelt|"
    r"wechselt\s+(?:den\s+)?besitzer|eröffne\w*|bau(?:t|en)|entwickel(?:t|n)|errichte\w*|baustart|spatenstich|"
    r"richtfest|grundsteinlegung|fertiggestellt)\b",
    re.I,
)
INDUSTRIAL_TAXONOMY = {"industrial-and-logistics", "data-center-property", "data-centers", "industrial"}


def _nearest(regex: re.Pattern[str], text: str, *, from_end: bool) -> int | None:
    hits = list(regex.finditer(text))
    if not hits:
        return None
    return len(text) - hits[-1].end() if from_end else hits[0].start()


@dataclass
class _Score:
    value: float = 0.0
    rules: list[str] = field(default_factory=list)

    def add(self, weight: float, rule: str) -> None:
        self.value += weight
        self.rules.append(rule)


def parse_number_de(text: str) -> float | None:
    cleaned = re.sub(r"[\s  ]", "", text)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_areas(text: str) -> list[dict[str, Any]]:
    """All area mentions in m², each tagged ``plot`` or ``building`` by its left-hand context."""
    found: list[dict[str, Any]] = []
    for m in _AREA_RE.finditer(text):
        value = parse_number_de(m.group("num"))
        if value is None:
            continue
        unit = m.group("unit").lower()
        sqm = value * 10_000 if unit in {"ha", "hektar"} else value
        if not 10 <= sqm <= 50_000_000:
            continue
        left = text[max(0, m.start() - 60) : m.start()]
        right = text[m.end() : m.end() + 28]
        # the keyword closest to the number decides: "Nutzfläche von 170.000 m², errichtet auf einem
        # 110.000 m² großen Grundstück" → 170 000 = building, 110 000 = plot
        candidates = [
            (d, kind)
            for d, kind in (
                (_nearest(_PLOT_WORDS, left, from_end=True), "plot"),
                (_nearest(_BUILDING_WORDS, left, from_end=True), "building"),
                (_nearest(_PLOT_WORDS, right, from_end=False), "plot"),
                (_nearest(_BUILDING_WORDS, right, from_end=False), "building"),
            )
            if d is not None
        ]
        if unit in {"ha", "hektar"}:
            kind = "plot"
        elif candidates:
            kind = min(candidates, key=lambda c: (c[0], c[1] != "plot"))[1]
        else:
            kind = "unknown"
        found.append({"sqm": sqm, "kind": kind, "snippet": text[max(0, m.start() - 40) : m.end() + 30].strip()})
    return found


def _snippet(regex: re.Pattern[str], text: str, width: int = 50) -> str | None:
    m = regex.search(text)
    if not m:
        return None
    return text[max(0, m.start() - width) : m.end() + width].strip()


def _near(text: str, a: re.Pattern[str], b: re.Pattern[str], max_chars: int = 120) -> str | None:
    """Return a snippet where a match of ``a`` and of ``b`` occur within ``max_chars``."""
    b_positions = [m.start() for m in b.finditer(text)]
    if not b_positions:
        return None
    for m in a.finditer(text):
        if any(abs(m.start() - p) <= max_chars for p in b_positions):
            lo = max(0, min([m.start(), *[p for p in b_positions if abs(m.start() - p) <= max_chars]]) - 20)
            return text[lo : m.end() + 80].strip()
    return None


class SignalClassifier:
    def __init__(self, *, big_box_min_sqm: int = 10_000, min_confidence: float = 0.5) -> None:
        self.big_box_min_sqm = big_box_min_sqm
        self.min_confidence = min_confidence

    def classify(self, article: Article) -> Classification:
        headline = f"{article.title}. {article.subtitle or ''}"
        lead = " ".join(article.paragraphs[:2])
        focus = f"{headline} {lead}"
        categories = article.news_categories
        property_types = article.property_types
        evidence: dict[str, Any] = {
            "taxonomy_news": sorted(categories),
            "taxonomy_property_types": sorted(property_types),
        }

        areas = extract_areas(f"{headline}\n{article.body_text}")
        focus_areas = extract_areas(focus)
        plot_area = max((a["sqm"] for a in areas if a["kind"] == "plot"), default=None)
        building_area = max(
            (a["sqm"] for a in focus_areas if a["kind"] in {"building", "unknown"}), default=None
        ) or max((a["sqm"] for a in areas if a["kind"] == "building"), default=None)
        title_letting = bool(_LETTING_RE.search(article.title)) or (
            bool(_LETTING_RE.search(lead[:400])) and not _ACQUISITION_RE.search(article.title)
        )
        # The size of the *deal* matters: "bezieht 2.050 m² im 35.000 m² großen Objekt" is not big-box.
        title_areas = [a["sqm"] for a in extract_areas(article.title) if a["kind"] != "plot"]
        letting_area = _LETTING_AREA_RE.search(focus) if title_letting else None
        if title_areas:
            deal_area: float | None = max(title_areas)
        elif letting_area:
            deal_area = parse_number_de(letting_area.group("num"))
        else:
            deal_area = building_area
        if areas:
            evidence["areas"] = areas[:8]
        evidence["deal_area_sqm"] = deal_area

        def reject(reason: str) -> Classification:
            return Classification(
                signal_type=None,
                confidence=0.0,
                area_sqm=deal_area,
                plot_area_sqm=plot_area,
                evidence=evidence,
                rejection_reason=reason,
            )

        # --- hard exclusions ----------------------------------------------------------------
        if "people" in categories and "transactions" not in categories:
            return reject("personnel_news")
        is_transaction_news = "transactions" in categories
        deal_verb = _DEAL_VERB_RE.search(article.title)
        research_title = _RESEARCH_RE.search(article.title)
        ownership_or_build_verb = (
            _ACQUISITION_RE.search(article.title)
            or _DISPOSAL_RE.search(article.title)
            or _DEVELOPMENT_RE.search(article.title)
        )
        insight_page = "content-type/marketing-article/insight" in article.taxonomy
        research_tagged = ("research" in categories or insight_page) and not is_transaction_news
        if (research_title and not deal_verb) or (research_tagged and not ownership_or_build_verb):
            evidence["research_terms"] = sorted({m.lower() for m in _RESEARCH_RE.findall(headline)})[:5]
            return reject("market_research")
        if not is_transaction_news and not deal_verb:
            # corporate news, cooperations, commentary: no single, locatable deal or scheme
            return reject("not_a_single_deal")

        # --- commercial land disposal --------------------------------------------------------
        land = _Score()
        land_in_title = _LAND_TITLE_RE.search(article.title)
        land_deal = _near(focus, _LAND_RE, _TRANSACTION_RE, 120)
        if land_in_title and _TRANSACTION_RE.search(article.title):
            land.add(0.6, "title:land+transaction")
        elif land_in_title and land_deal:
            land.add(0.5, "title:land+lead:transaction")
        elif land_deal:
            land.add(0.35, "lead:land+transaction")
        if land.value:
            if is_transaction_news:
                land.add(0.15, "taxonomy:news/transactions")
            if plot_area:
                land.add(0.1, "plot_area_stated")
            if _VACANT_RE.search(focus):
                land.add(0.1, "vacant/brownfield/zoning")
            if _DEVELOPMENT_RE.search(focus):
                land.add(0.05, "development_intent")
            if _COMMERCIAL_USE_RE.search(focus):
                land.add(0.05, "commercial_use_stated")
            building_object = _BUILDING_OBJECT_RE.search(article.title)
            if building_object and not land_in_title:
                land.add(-0.45, f"title_object_is_building:{building_object.group(0)}")
            if not land_in_title and _STANDING_BUILDING_RE.search(lead):
                land.add(-0.35, "plot_carries_standing_building")
            residential = len(_RESIDENTIAL_RE.findall(focus))
            commercial = len(_COMMERCIAL_USE_RE.findall(focus))
            if residential and residential >= commercial:
                land.add(-0.6, "residential_land")
            if title_letting:
                land.add(-0.6, "letting_not_disposal")
            evidence["land_snippet"] = land_deal or _snippet(_LAND_RE, article.title)

        # --- big-box industrial development ---------------------------------------------------
        dev = _Score()
        industrial_tax = bool(property_types & INDUSTRIAL_TAXONOMY)
        industrial_head = _INDUSTRIAL_RE.search(headline)
        industrial_lead = _INDUSTRIAL_RE.search(lead)
        if industrial_tax or industrial_head or industrial_lead:
            if industrial_tax:
                dev.add(0.3, f"taxonomy:{','.join(sorted(property_types & INDUSTRIAL_TAXONOMY))}")
            if industrial_head:
                dev.add(0.25, f"headline:industrial:{industrial_head.group(0)}")
            elif industrial_lead:
                dev.add(0.15, f"lead:industrial:{industrial_lead.group(0)}")
            development = _DEVELOPMENT_RE.search(headline)
            development_lead = None if development else _near(lead, _DEVELOPMENT_RE, _INDUSTRIAL_RE, 200)
            if development:
                dev.add(0.35, f"headline:development:{development.group(0)}")
                evidence["development_snippet"] = _snippet(_DEVELOPMENT_RE, headline)
            elif development_lead:
                dev.add(0.25, "lead:development")
                evidence["development_snippet"] = development_lead
            big_box = deal_area is not None and deal_area >= self.big_box_min_sqm
            if big_box:
                dev.add(0.25, f"big_box_area>={self.big_box_min_sqm}")
            if is_transaction_news:
                dev.add(0.05, "taxonomy:news/transactions")
            if _ACQUISITION_RE.search(article.title) or _DISPOSAL_RE.search(article.title):
                dev.add(0.1, "title:ownership_change")
            has_dev = bool(development or development_lead)
            if not has_dev and not big_box:
                dev.add(-0.5, "neither_development_nor_big_box")
            if title_letting and not big_box:
                dev.add(-0.4, "letting_below_big_box_threshold")
            elif not development and deal_area is not None and deal_area < self.big_box_min_sqm * 0.6:
                # a development cue only in the lead rarely concerns a small traded asset
                dev.add(-0.4, "deal_well_below_big_box_size")
            if property_types and not industrial_tax:
                # JLL's taxonomy is occasionally wrong ("Möbelfabrik" tagged office) — an explicit
                # industrial headline outweighs it, lead-only mentions do not.
                penalty = 0.1 if industrial_head else 0.35
                dev.add(-penalty, f"non_industrial_property_type:{','.join(sorted(property_types))}")

        land_conf = round(max(0.0, min(1.0, land.value)), 3)
        dev_conf = round(max(0.0, min(1.0, dev.value)), 3)
        evidence["rules"] = {"commercial_land_disposal": land.rules, "big_box_industrial_development": dev.rules}
        evidence["scores"] = {"commercial_land_disposal": land_conf, "big_box_industrial_development": dev_conf}

        eligible = [
            (c, s)
            for c, s in (
                (land_conf, SignalType.COMMERCIAL_LAND_DISPOSAL),
                (dev_conf, SignalType.BIG_BOX_INDUSTRIAL_DEVELOPMENT),
            )
            if c >= self.min_confidence
        ]
        if not eligible:
            if not land.rules and not dev.rules:
                domain = ",".join(sorted(property_types)) or "unknown"
                return reject(f"out_of_domain:{domain}")
            return reject("below_confidence_threshold")
        # A land trade named in the title is the triggering event; otherwise the higher score wins and
        # ties go to big-box (the land evidence then only came from the lead).
        if land_in_title and land_conf >= self.min_confidence:
            confidence, signal_type = land_conf, SignalType.COMMERCIAL_LAND_DISPOSAL
        else:
            confidence, signal_type = max(
                eligible, key=lambda c: (c[0], c[1] == SignalType.BIG_BOX_INDUSTRIAL_DEVELOPMENT)
            )
        secondary = [s.value for _, s in eligible if s != signal_type]
        if secondary:
            evidence["also_matches"] = secondary

        return Classification(
            signal_type=signal_type,
            confidence=confidence,
            deal_type=self._deal_type(article.title, focus),
            area_sqm=deal_area,
            plot_area_sqm=plot_area,
            evidence=evidence,
        )

    @staticmethod
    def _deal_type(title: str, text: str) -> str | None:
        """Prefer what the headline says; fall back to the lead."""
        for source in (title, text):
            if _LETTING_RE.search(source):
                return "letting"
            if _ACQUISITION_RE.search(source):
                return "acquisition"
            if _DISPOSAL_RE.search(source):
                return "disposal"
            if _DEVELOPMENT_RE.search(source):
                return "development"
        return None
