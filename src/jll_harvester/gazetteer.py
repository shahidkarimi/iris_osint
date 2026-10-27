"""Offline German gazetteer (GeoNames, CC BY 4.0) and place-mention extraction.

Extraction is context-driven to stay precise on German prose, where many place names are also
ordinary words ("Lage", "Halle", "Mitte", "Essen"):

* dateline            ``MÖNCHENGLADBACH, 16. Juli 2026 –``
* street address      ``am Regioparkring 4``, ``an der Sophie-Germain-Straße``
* hyphenated district ``Bonn-Auerberg``, ``München-Neuperlach``
* district phrase     ``im Bonner Stadtteil Auerberg``
* preposition         ``in/bei/nahe/aus <Place>`` (title & body)
* demonym             ``Kölner``, ``Münchner`` … + noun
* region              ``in Oberbayern`` (never resolvable to a point — recorded only)
"""

from __future__ import annotations

import csv
import gzip
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from .models import Article, LocationCandidate

GERMAN_STATES_AND_REGIONS = {
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen", "Hamburg", "Hessen",
    "Mecklenburg-Vorpommern", "Niedersachsen", "Nordrhein-Westfalen", "NRW", "Rheinland-Pfalz",
    "Saarland", "Sachsen", "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
    "Oberbayern", "Niederbayern", "Oberpfalz", "Oberfranken", "Mittelfranken", "Unterfranken",
    "Schwaben", "Ostwestfalen", "Ostwestfalen-Lippe", "Westfalen", "Rheinland", "Ruhrgebiet",
    "Münsterland", "Sauerland", "Emsland", "Ostfriesland", "Allgäu", "Bodensee", "Rhein-Main",
    "Rhein-Neckar", "Rhein-Ruhr", "Lausitz", "Vogtland", "Erzgebirge", "Eifel", "Hunsrück",
    "Pfalz", "Taunus", "Wetterau", "Breisgau", "Schwarzwald", "Harz", "Uckermark", "Prignitz",
    "Altmark", "Spreewald", "Nordhessen", "Südhessen", "Mitteldeutschland", "Norddeutschland",
    "Süddeutschland", "Westdeutschland", "Ostdeutschland", "Deutschland",
}  # fmt: skip
# City-states and states that share a name with a city resolve to the city.
_CITY_STATES = {"Berlin", "Hamburg", "Bremen"}

# Place names that are also common German words; only accepted in dateline/address/district
# context or with an explicit qualifier.
AMBIGUOUS_WORDS = {
    "Lage", "Halle", "Mitte", "Essen", "Weil", "Hof", "Bad", "Werne", "Senden", "Laufen", "Rain",
    "Au", "Berg", "Wald", "Mark", "Ort", "Park", "Stadt", "Nord", "Süd", "Ost", "West", "Hafen",
    "Zentrum", "City", "Innenstadt", "Altstadt", "Neustadt", "Damm", "Brück", "Werben", "Kirchen",
    "Lauf", "Heide", "Moos", "Rot", "Linden", "Ahlen", "Bergen", "Wiesen", "Elster", "Treuen",
    "Hagen", "Marl", "Stade", "Hamm", "Emden", "Kaufungen", "Bruchsal", "Pinneberg", "Kassel",
    "Holzkirchen", "Haar", "Grafing", "Wedding", "Moers", "Heiden", "Ems", "Zell", "Glücksburg",
    "Sonne", "Euro", "Haus", "Teltow", "Waren", "Lichtenstein", "Weiden", "Werder", "Schwalbach",
    "Rees", "Ratingen", "Neuss", "Garching", "Bahnhof", "Flughafen", "Messe", "Logistik",
}  # fmt: skip
# Of the above, the ones that are *safe* after "in"/"bei" because they are real cities and the
# common-word reading is unlikely in that position.
SAFE_AFTER_PREPOSITION = {
    "Essen", "Hagen", "Marl", "Stade", "Hamm", "Emden", "Kassel", "Bruchsal", "Pinneberg",
    "Holzkirchen", "Haar", "Grafing", "Moers", "Teltow", "Ratingen", "Neuss", "Garching", "Weiden",
    "Werder", "Ahlen", "Kaufungen", "Werne", "Senden", "Laufen", "Lichtenstein", "Schwalbach", "Rees",
    "Halle",
}  # fmt: skip
IRREGULAR_DEMONYMS = {
    "Münchner": "München", "Münchener": "München", "Dresdner": "Dresden", "Bremer": "Bremen",
    "Hannoveraner": "Hannover", "Hannoverschen": "Hannover", "Frankfurter": "Frankfurt am Main",
    "Hallenser": "Halle (Saale)", "Hallesche": "Halle (Saale)", "Görlitzer": "Görlitz",
    "Wiesbadener": "Wiesbaden", "Osnabrücker": "Osnabrück", "Erfurter": "Erfurt",
    "Eschborner": "Eschborn", "Heidelberger": "Heidelberg", "Bielefelder": "Bielefeld",
}  # fmt: skip
SHORT_FORMS = {
    "Frankfurt": "Frankfurt am Main", "Halle/Saale": "Halle (Saale)", "Halle (Saale)": "Halle (Saale)",
    "Frankfurt/Main": "Frankfurt am Main", "Frankfurt a. M.": "Frankfurt am Main",
    "Frankfurt (Oder)": "Frankfurt (Oder)", "Mülheim an der Ruhr": "Mülheim (Ruhr)",
    "Mülheim": "Mülheim (Ruhr)", "Offenbach": "Offenbach am Main", "Freiburg": "Freiburg im Breisgau",
    "Ludwigshafen": "Ludwigshafen am Rhein", "Monheim": "Monheim am Rhein",
    "Brandenburg an der Havel": "Brandenburg an der Havel", "Neumarkt": "Neumarkt in der Oberpfalz",
    "Landsberg": "Landsberg am Lech", "Weil am Rhein": "Weil am Rhein",
}  # fmt: skip

_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
_STREET_SUFFIX = (
    r"(?:straße|strasse|str\.|weg|allee|ring|platz|damm|ufer|chaussee|gasse|steig|stieg|"
    r"kai|landstraße|promenade|bogen|graben|wall|zeile|pfad)"
)
# "Regioparkring 4", "Sophie-Germain-Straße", "Große Elbstraße 273-275", "Adresse Grundfeld 1"
_STREET_RE = re.compile(
    rf"(?P<street>(?:[A-ZÄÖÜ][a-zäöüß]+[-\s])?(?:[A-ZÄÖÜ][a-zäöüß]+-)*[A-ZÄÖÜ][a-zäöüß]*{_STREET_SUFFIX})"
    r"(?:\s+(?P<number>\d{1,3}[a-z]?(?:\s*[-–]\s*\d{1,3}[a-z]?)?))?(?![\wäöüß])"
)
_ADDRESS_KEYWORD_RE = re.compile(
    r"\b(?:Adresse|Anschrift)\s+(?P<street>[A-ZÄÖÜ][\wäöüß\-]+(?:\s[A-ZÄÖÜ][\wäöüß\-]+)?)\s+(?P<number>\d{1,3}[a-z]?)\b"
)
_STREET_CONTEXT_RE = re.compile(r"(?:\b(?:am|an\s+der|an\s+dem|in\s+der|auf\s+der|auf\s+dem)\s+)$")
_STRONG_STREET_RE = re.compile(r"(?:straße|strasse|allee|weg|ring|platz|damm|ufer|chaussee)$", re.I)
_POSTCODE_RE = re.compile(r"\b(?P<plz>[0-9]{5})\s+(?P<city>[A-ZÄÖÜ][\wäöüß.\- ]{2,40}?)\b")
_HYPHEN_DISTRICT_RE = re.compile(r"\b(?P<city>[A-ZÄÖÜ][\wäöüß]+)-(?P<district>[A-ZÄÖÜ][\wäöüß]+)\b")
_DISTRICT_PHRASE_RE = re.compile(
    r"\b(?:im|in\s+dem|im\s+(?P<demonym>[A-ZÄÖÜ][\wäöüß]+er))\s*(?:Stadtteil|Ortsteil|Stadtbezirk|Bezirk|Quartier)\s+"
    r"(?P<district>[A-ZÄÖÜ][\wäöüß\-]+)"
)
_PREPOSITION_RE = re.compile(
    r"\b(?P<prep>in|bei|nahe|nahe\s+bei|nördlich\s+von|südlich\s+von|östlich\s+von|westlich\s+von|"
    r"im\s+Raum|aus|nach|von|zwischen|im\s+(?:nord|süd|ost|west)\w*\s+von)\s+"
    r"(?P<place>[A-ZÄÖÜ][\wäöüß]+(?:[-\s](?:am|an\s+der|im|in\s+der|ob\s+der|bei)\s+[A-ZÄÖÜ][\wäöüß]+|"
    r"\s*\((?:Saale|Oder|Main|Ruhr)\)|/(?:Main|Saale|Oder)|-[A-ZÄÖÜ][\wäöüß]+)?)"
)
_ADJ_PLACE_RE = re.compile(
    r"\b(?:im|in|ins|am)\s+(?:\w+\s+)?(?P<adj>[a-zäöü]+(?:en|ischen|ischer))\s+(?P<place>[A-ZÄÖÜ][\wäöüß]+)"
)
_DEMONYM_RE = re.compile(r"\b(?P<demonym>[A-ZÄÖÜ][\wäöüß]+?(?:er|ner|ener))\s+(?=[A-ZÄÖÜ„\"])")


def fold(text: str) -> str:
    """Case- and accent-insensitive key: ``"Mönchengladbach"`` → ``"moenchengladbach"``."""
    text = text.translate(_UMLAUT).lower()
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).strip()


@dataclass(frozen=True)
class GazetteerEntry:
    geonameid: int
    kind: str  # place | district | region
    name: str
    lat: float
    lon: float
    fcode: str
    admin1: str
    population: int


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371.0 * math.hypot(dlat, dlon)


class Gazetteer:
    def __init__(self, entries: list[GazetteerEntry]) -> None:
        self._index: dict[str, dict[str, list[GazetteerEntry]]] = defaultdict(lambda: defaultdict(list))
        for e in entries:
            keys = {fold(e.name)}
            base = re.split(r"\s+(?:am|an der|im|in der|ob der|bei)\s+|\s*\(", e.name)[0]
            if e.kind == "place" and base != e.name and e.population >= 20_000:
                keys.add(fold(base))
            for k in keys:
                self._index[e.kind][k].append(e)
        for by_name in self._index.values():
            for lst in by_name.values():
                lst.sort(key=lambda x: -x.population)
        self.size = len(entries)

    @classmethod
    @lru_cache(maxsize=1)
    def bundled(cls) -> Gazetteer:
        path = resources.files("jll_harvester") / "data" / "gazetteer_de.tsv.gz"
        entries: list[GazetteerEntry] = []
        with path.open("rb") as raw, gzip.open(raw, "rt", encoding="utf-8") as fh:
            for row in csv.DictReader(fh, delimiter="\t"):
                entries.append(
                    GazetteerEntry(
                        geonameid=int(row["geonameid"]),
                        kind=row["kind"],
                        name=row["name"],
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                        fcode=row["fcode"],
                        admin1=row["admin1"],
                        population=int(row["population"] or 0),
                    )
                )
        return cls(entries)

    def places(self, name: str) -> list[GazetteerEntry]:
        name = SHORT_FORMS.get(name, name)
        return list(self._index["place"].get(fold(name), []))

    def districts(
        self, name: str, near: tuple[float, float] | None = None, radius_km: float = 25
    ) -> list[GazetteerEntry]:
        key = fold(name)
        hits = (
            self._index["district"].get(key, [])
            + self._index["locality"].get(key, [])
            + self._index["place"].get(key, [])
        )
        if near is None:
            return hits
        return sorted(
            (h for h in hits if distance_km(near, (h.lat, h.lon)) <= radius_km),
            key=lambda h: distance_km(near, (h.lat, h.lon)),
        )

    def is_region(self, name: str) -> bool:
        """True for states/regions. Names shared with a real town (Köln, Leipzig) are places."""
        if name in _CITY_STATES:
            return False
        if name in GERMAN_STATES_AND_REGIONS:
            return True
        return bool(self._index["region"].get(fold(name))) and not self.places(name)

    def demonym_to_place(self, word: str) -> str | None:
        if word in IRREGULAR_DEMONYMS:
            return IRREGULAR_DEMONYMS[word]
        for suffix in ("er",):
            stem = word[: -len(suffix)]
            if len(stem) >= 3 and (hits := self.places(stem)) and hits[0].population >= 30_000:
                return hits[0].name
        return None


# ---------------------------------------------------------------------------------------------
# Mention extraction
# ---------------------------------------------------------------------------------------------
_ORIGIN_WEIGHT = {
    "address": 1.0, "title": 0.9, "district": 0.85, "facility": 0.8, "body": 0.6, "demonym": 0.55, "dateline": 0.4,
}  # fmt: skip
# "Logistikpark Dombühl", "Segro Park Dortmund", "Gewerbegebiet Butzweilerhof", "Stadt Ratingen"
_FACILITY_PLACE_RE = re.compile(
    r"\b(?:\w*(?i:park|parks|zentrum|quartier|areal|campus|center|hafen|gewerbegebiet|industriegebiet)|Stadt|Gemeinde|Projekt)"
    r"\s+(?P<place>[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?)\b"
)
_WEAK_CONTEXT_RE = re.compile(
    r"(?:benachbart\w*|nahegelegen\w*|entfernt\w*|umgebung|ehemals|bisher\w*|aktuell\w*|vom\s+standort)\s*$", re.I
)


@dataclass
class _Mention:
    candidate: LocationCandidate
    text_index: int
    start: int


def _titlecase_dateline(place: str) -> str:
    # "MÖNCHENGLADBACH" → "Mönchengladbach", "FRANKFURT/MAIN" → "Frankfurt/Main"
    return "-".join("/".join(part.capitalize() for part in seg.split("/")) for seg in place.strip().split("-"))


def rank_towns(candidates: list[LocationCandidate]) -> list[tuple[str, float, set[str]]]:
    """Aggregate mentions per municipality: best mention + 0.1 per additional distinct origin."""
    towns: dict[str, tuple[float, set[str]]] = {}
    for c in candidates:
        if not c.municipality or c.kind == "region":
            continue
        best, origins = towns.get(c.municipality, (0.0, set()))
        towns[c.municipality] = (max(best, c.score), origins | {c.origin})
    ranked = [(name, min(1.0, best + 0.1 * (len(origins) - 1)), origins) for name, (best, origins) in towns.items()]
    return sorted(ranked, key=lambda t: -t[1])


def extract_candidates(article: Article, gazetteer: Gazetteer) -> list[LocationCandidate]:
    """Collect place mentions with provenance. Scores favour precise, property-specific mentions."""
    mentions: list[_Mention] = []
    texts: list[tuple[str, str]] = [("title", article.title)]
    if article.subtitle:
        texts.append(("title", article.subtitle))
    texts.extend(("body", p) for p in article.paragraphs[:3])

    def add(c: LocationCandidate, text_index: int = -1, start: int = 0) -> None:
        c.score = round(c.score or _ORIGIN_WEIGHT.get(c.origin, 0.3), 3)
        mentions.append(_Mention(c, text_index, start))

    def town(name: str, min_population: int = 1_000) -> str | None:
        if name in AMBIGUOUS_WORDS and name not in SAFE_AFTER_PREPOSITION:
            return None
        hits = gazetteer.places(name)
        return hits[0].name if hits and hits[0].population >= min_population else None

    # 1) dateline — often the JLL office issuing the release, hence the lowest weight
    if article.dateline_place:
        for raw in re.split(r"\s*/\s*|\s+und\s+|\s*,\s*", article.dateline_place):
            name = _titlecase_dateline(raw)
            if name not in {"Main", "Saale", "Oder"} and (hit := town(name)):
                add(LocationCandidate(text=raw.strip(), kind="place", origin="dateline", municipality=hit))

    for idx, (origin, text) in enumerate(texts):
        # 2) explicit postcode + city
        for m in _POSTCODE_RE.finditer(text):
            if hit := town(m.group("city").strip()):
                add(
                    LocationCandidate(
                        text=m.group(0),
                        kind="place",
                        origin="address",
                        municipality=hit,
                        postal_code=m.group("plz"),
                        score=0.95,
                    ),
                    idx,
                    m.start(),
                )

        # 3) hyphenated district "Bonn-Auerberg", "Köln-Ossendorf"
        for m in _HYPHEN_DISTRICT_RE.finditer(text):
            city_hits = gazetteer.places(m.group("city"))
            if not city_hits or city_hits[0].population < 20_000:
                continue
            if gazetteer.districts(m.group("district"), near=(city_hits[0].lat, city_hits[0].lon)):
                add(
                    LocationCandidate(
                        text=m.group(0),
                        kind="district",
                        origin="district" if origin == "body" else origin,
                        municipality=city_hits[0].name,
                        district=m.group("district"),
                    ),
                    idx,
                    m.start(),
                )

        # 4) "im Bonner Stadtteil Auerberg"
        for m in _DISTRICT_PHRASE_RE.finditer(text):
            city = gazetteer.demonym_to_place(m.group("demonym")) if m.group("demonym") else None
            add(
                LocationCandidate(
                    text=m.group(0), kind="district", origin="district", municipality=city, district=m.group("district")
                ),
                idx,
                m.start(),
            )

        # 5) prepositional place mentions
        for m in _PREPOSITION_RE.finditer(text):
            raw = m.group("place")
            # "Feldkirchen bei München" → Feldkirchen; "Bonn-Beuel" → Bonn (district handled above)
            options = [raw, re.split(r"\s+(?:am|an der|im|in der|ob der|bei)\s+", raw)[0], raw.split("-")[0]]
            name = next((o for o in options if gazetteer.places(o)), options[-1])
            if gazetteer.is_region(name):
                add(LocationCandidate(text=m.group(0), kind="region", origin=origin, score=0.2), idx, m.start())
                continue
            weak = origin == "body" and m.group("prep") in {"aus", "von", "nach", "zwischen"}
            weak = weak or bool(
                _WEAK_CONTEXT_RE.search(text[max(0, m.start() - 25) : m.start() + len(m.group("prep")) + 1])
            )
            weak = weak or bool(re.match(r"\w+\s+(?:benachbart|nahegelegen)", m.group(0)))
            # "bei Berlin", "nahe Berlin", "im Raum Köln": the site is *near* the town, not in it
            near = name == raw and bool(
                re.match(r"(?:bei|nahe|im\s+raum|\w+lich\s+von|im\s+\w+\s+von)$", m.group("prep"), re.I)
            )
            if hit := town(name, 20_000 if weak else 1_000):
                add(
                    LocationCandidate(
                        text=m.group(0),
                        kind="place",
                        origin=f"{origin}-near" if near else origin,
                        municipality=hit,
                        score=_ORIGIN_WEIGHT[origin] - (0.25 if weak else 0.0) - (0.3 if near else 0.0),
                    ),
                    idx,
                    m.start(),
                )

        # 5b) "im unterfränkischen Elfershausen" — "im benachbarten Eschweiler" is a different place
        for m in _ADJ_PLACE_RE.finditer(text):
            if gazetteer.is_region(m.group("place")) or not (hit := town(m.group("place"))):
                continue
            neighbour = re.match(r"benachbart|nahegelegen|angrenzend", m.group("adj"))
            add(
                LocationCandidate(
                    text=m.group(0), kind="place", origin=origin, municipality=hit, score=0.35 if neighbour else 0.0
                ),
                idx,
                m.start(),
            )

        # 5c) facility names carrying the town: "Logistikpark Dombühl", "Segro Park Dortmund"
        for m in _FACILITY_PLACE_RE.finditer(text):
            if not gazetteer.is_region(m.group("place")) and (hit := town(m.group("place"))):
                add(
                    LocationCandidate(text=m.group(0), kind="place", origin="facility", municipality=hit),
                    idx,
                    m.start(),
                )

        # 6) demonyms "Kölner Dom", "Aachener Standort"
        for m in _DEMONYM_RE.finditer(text):
            if place := gazetteer.demonym_to_place(m.group("demonym")):
                add(
                    LocationCandidate(text=m.group(0).strip(), kind="place", origin="demonym", municipality=place),
                    idx,
                    m.start(),
                )

        # 7) street addresses — need a house number, or "an der …straße" style context
        for m in _STREET_RE.finditer(text):
            street, number = m.group("street").strip(), m.group("number")
            if street.lower().startswith(("straße", "strasse")) or len(street) < 6:
                continue
            if number is None and not (
                _STRONG_STREET_RE.search(street) and _STREET_CONTEXT_RE.search(text[max(0, m.start() - 12) : m.start()])
            ):
                continue
            add(
                LocationCandidate(
                    text=m.group(0).strip(),
                    kind="address",
                    origin="address",
                    street=street,
                    house_number=number,
                    score=1.0 if number else 0.8,
                ),
                idx,
                m.start(),
            )
        for m in _ADDRESS_KEYWORD_RE.finditer(text):
            add(
                LocationCandidate(
                    text=m.group(0).strip(),
                    kind="address",
                    origin="address",
                    street=m.group("street"),
                    house_number=m.group("number"),
                ),
                idx,
                m.start(),
            )

    # Attach a municipality to streets/districts that lack one: the nearest place mentioned before
    # it in the same paragraph ("Aachener Standort … in der Neuenhofstraße 180"), else the best town.
    candidates = [m.candidate for m in mentions]
    ranking = rank_towns([c for c in candidates if c.origin != "dateline"]) or rank_towns(candidates)
    best_town = ranking[0][0] if ranking else None
    for mention in mentions:
        c = mention.candidate
        if c.kind not in {"address", "district"} or c.municipality:
            continue
        preceding = [
            o
            for o in mentions
            if o.text_index == mention.text_index
            and o.start < mention.start
            and o.candidate.kind == "place"
            and o.candidate.municipality
            and o.candidate.score >= 0.5
        ]
        c.municipality = max(preceding, key=lambda o: o.start).candidate.municipality if preceding else best_town
    return [c for c in candidates if c.kind != "address" or c.municipality]
