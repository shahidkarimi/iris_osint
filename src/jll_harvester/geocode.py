"""Location resolution → validated EPSG:25832 points.

Pipeline: place mentions (:mod:`gazetteer`) → choose the property location → geocode at the most
precise level available → validate → project to ETRS89 / UTM 32N (EPSG:25832).

Rules that keep bad coordinates out of staging:

* only a single, unambiguous property location yields a point; multi-site deals ("Frankfurt und
  Mannheim", portfolios) are ``ambiguous`` and keep their candidates instead of a guessed point;
* region-only mentions ("in Oberbayern") are ``unresolved`` — a region centroid is not a site;
* every coordinate must be finite, inside the German bounding box, in the expected municipality
  (≤ ``max_offset_km`` from its gazetteer centroid) and inside the EPSG:25832 area of use,
  otherwise the point is dropped and the record is marked ``invalid`` with the reason.
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pyproj import Transformer

from .gazetteer import Gazetteer, distance_km, extract_candidates, rank_towns
from .models import (
    Article,
    GeocodePrecision,
    GeocodeStatus,
    LocationCandidate,
    LocationResolution,
)

log = logging.getLogger(__name__)

# Germany incl. a small margin (lon/lat, WGS84) and the matching EPSG:25832 envelope.
DE_BBOX = (5.5, 47.0, 15.6, 55.2)
UTM32_BOUNDS = (200_000.0, 5_200_000.0, 960_000.0, 6_120_000.0)
_TO_25832 = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)

_PRECISION_RANK = {
    GeocodePrecision.ADDRESS: 0,
    GeocodePrecision.STREET: 1,
    GeocodePrecision.DISTRICT: 2,
    GeocodePrecision.MUNICIPALITY: 3,
}


@dataclass
class GeoHit:
    lat: float
    lon: float
    precision: GeocodePrecision
    provider: str
    ref: str | None = None
    label: str | None = None
    confidence: float = 0.5
    country_code: str | None = "de"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class GeocodeQuery:
    precision: GeocodePrecision
    municipality: str
    street: str | None = None
    house_number: str | None = None
    district: str | None = None
    postal_code: str | None = None

    @property
    def key(self) -> str:
        parts = [
            self.precision.value,
            self.street or "",
            self.house_number or "",
            self.district or "",
            self.postal_code or "",
            self.municipality,
        ]
        return "|".join(p.strip().lower() for p in parts)

    @property
    def label(self) -> str:
        street = " ".join(p for p in (self.street, self.house_number) if p)
        return ", ".join(
            p for p in (street, self.district, f"{self.postal_code or ''} {self.municipality}".strip()) if p
        )


class Geocoder(Protocol):
    name: str

    def geocode(self, query: GeocodeQuery) -> GeoHit | None: ...


class GeocoderUnavailable(Exception):
    """The provider could not be reached (network, 5xx, rate limit) — distinct from 'not found'."""


# ---------------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------------
class GazetteerGeocoder:
    """Offline fallback: municipality / district centroids from the bundled GeoNames extract."""

    name = "geonames-offline"

    def __init__(self, gazetteer: Gazetteer) -> None:
        self.gazetteer = gazetteer

    def geocode(self, query: GeocodeQuery) -> GeoHit | None:
        places = self.gazetteer.places(query.municipality)
        if not places:
            return None
        town = places[0]
        if query.precision == GeocodePrecision.DISTRICT and query.district:
            districts = self.gazetteer.districts(query.district, near=(town.lat, town.lon))
            if districts:
                d = districts[0]
                return GeoHit(
                    d.lat,
                    d.lon,
                    GeocodePrecision.DISTRICT,
                    self.name,
                    f"geonames:{d.geonameid}",
                    f"{d.name}, {town.name}",
                    0.6,
                )
        if query.precision in {GeocodePrecision.DISTRICT, GeocodePrecision.MUNICIPALITY}:
            return GeoHit(
                town.lat,
                town.lon,
                GeocodePrecision.MUNICIPALITY,
                self.name,
                f"geonames:{town.geonameid}",
                town.name,
                0.5,
            )
        return None  # the gazetteer has no street-level data


class NominatimGeocoder:
    """OpenStreetMap Nominatim, used within its usage policy (≤ 1 req/s, identifying UA, cached)."""

    name = "nominatim"

    def __init__(
        self,
        *,
        base_url: str,
        user_agent: str,
        email: str | None = None,
        delay_seconds: float = 1.1,
        cache: MutableMapping[str, Any] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"User-Agent": user_agent, "Accept-Language": "de"},
            timeout=20.0,
            transport=transport,
        )
        self._email = email
        self._delay = delay_seconds
        self._last = 0.0
        self._sleep = sleep
        self.cache: MutableMapping[str, Any] = cache if cache is not None else {}
        self.calls = 0

    def close(self) -> None:
        self._client.close()

    def _params(self, query: GeocodeQuery) -> dict[str, str]:
        params = {"format": "jsonv2", "addressdetails": "1", "limit": "3", "countrycodes": "de"}
        if self._email:
            params["email"] = self._email
        if query.precision in {GeocodePrecision.ADDRESS, GeocodePrecision.STREET} and query.street:
            number = re.split(r"\s*[-–]\s*", query.house_number)[0] if query.house_number else None  # "4-6" → "4"
            params["street"] = " ".join(p for p in (number, query.street) if p)
            params["city"] = query.municipality
            if query.postal_code:
                params["postalcode"] = query.postal_code
        elif query.precision == GeocodePrecision.DISTRICT and query.district:
            params["q"] = f"{query.district}, {query.municipality}"
        else:
            params["city"] = query.municipality
            params["featureType"] = "settlement"
        return params

    def _search(self, query: GeocodeQuery) -> list[dict[str, Any]]:
        key = f"nominatim:{query.key}"
        if key in self.cache:
            cached = self.cache[key]
            return list(cached) if isinstance(cached, list) else []
        wait = self._delay - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        try:
            resp = self._client.get("/search", params=self._params(query))
        except httpx.HTTPError as exc:
            raise GeocoderUnavailable(str(exc)) from exc
        if resp.status_code in {429, 500, 502, 503, 504}:
            raise GeocoderUnavailable(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise GeocoderUnavailable(f"HTTP {resp.status_code}: {resp.text[:120]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise GeocoderUnavailable("malformed JSON from Nominatim") from exc
        results = data if isinstance(data, list) else []
        self.cache[key] = results
        return results

    def geocode(self, query: GeocodeQuery) -> GeoHit | None:
        for item in self._search(query):
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            address = item.get("address") or {}
            precision = query.precision
            if precision == GeocodePrecision.ADDRESS and not address.get("house_number"):
                precision = GeocodePrecision.STREET
            return GeoHit(
                lat=lat,
                lon=lon,
                precision=precision,
                provider=self.name,
                ref=f"osm:{item.get('osm_type', '?')}/{item.get('osm_id', '?')}",
                label=item.get("display_name"),
                confidence=float(item.get("importance") or 0.5),
                country_code=(address.get("country_code") or "").lower() or None,
                raw={k: item.get(k) for k in ("place_id", "osm_type", "osm_id", "category", "type", "addresstype")},
            )
        return None


# ---------------------------------------------------------------------------------------------
# Validation & projection
# ---------------------------------------------------------------------------------------------
class InvalidCoordinate(ValueError):
    pass


def project_25832(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 lon/lat → EPSG:25832 x/y, rejecting anything outside Germany."""
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise InvalidCoordinate("non-finite coordinate")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise InvalidCoordinate(f"coordinate out of WGS84 range ({lon}, {lat})")
    min_lon, min_lat, max_lon, max_lat = DE_BBOX
    if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
        raise InvalidCoordinate(f"coordinate outside Germany ({lon:.5f}, {lat:.5f})")
    x, y = _TO_25832.transform(lon, lat)
    min_x, min_y, max_x, max_y = UTM32_BOUNDS
    if not (math.isfinite(x) and math.isfinite(y) and min_x <= x <= max_x and min_y <= y <= max_y):
        raise InvalidCoordinate(f"projected coordinate outside EPSG:25832 envelope ({x}, {y})")
    return round(x, 2), round(y, 2)


# ---------------------------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------------------------
@dataclass
class _Town:
    name: str
    score: float
    origins: set[str]
    lat: float
    lon: float


class LocationResolver:
    def __init__(
        self,
        gazetteer: Gazetteer,
        geocoders: list[Geocoder],
        *,
        max_offset_km: float = 30.0,
        ambiguity_margin: float = 0.1,
    ) -> None:
        self.gazetteer = gazetteer
        self.geocoders = geocoders
        self.max_offset_km = max_offset_km
        self.ambiguity_margin = ambiguity_margin
        self.provider_failures: list[str] = []

    # -- choose -----------------------------------------------------------------------------
    def _towns(self, candidates: list[LocationCandidate]) -> list[_Town]:
        towns: list[_Town] = []
        for name, score, origins in rank_towns(candidates):
            hits = self.gazetteer.places(name)
            if hits:
                towns.append(_Town(hits[0].name, score, origins, hits[0].lat, hits[0].lon))
        return towns

    def resolve(self, article: Article) -> LocationResolution:
        candidates = extract_candidates(article, self.gazetteer)
        trail = [c.model_dump(exclude_none=True) for c in sorted(candidates, key=lambda c: -c.score)[:12]]
        towns = self._towns(candidates)
        regions = sorted({c.text for c in candidates if c.kind == "region"})

        if not towns:
            note = f"region-level mention only: {', '.join(regions)}" if regions else "no location mention found"
            return LocationResolution(
                status=GeocodeStatus.UNRESOLVED,
                candidates=trail,
                notes=[note],
                location_text=regions[0] if regions else None,
            )

        top = towns[0]
        rivals = [
            t
            for t in towns[1:]
            if t.score >= top.score - self.ambiguity_margin
            and t.origins - {"dateline", "demonym"}
            and distance_km((top.lat, top.lon), (t.lat, t.lon)) > self.max_offset_km
        ]
        if rivals and top.origins - {"dateline"}:
            names = [top.name, *[r.name for r in rivals]]
            return LocationResolution(
                status=GeocodeStatus.AMBIGUOUS,
                location_text=" / ".join(names),
                municipality=None,
                candidates=trail,
                notes=[f"several equally plausible locations: {', '.join(names)}"],
            )

        notes: list[str] = []
        if top.origins == {"dateline"}:
            notes.append("location taken from dateline only (may be the issuing JLL office)")
        elif top.origins <= {"title-near", "body-near", "dateline", "demonym"} and top.origins & {
            "title-near",
            "body-near",
        }:
            notes.append(f"site is only described as near {top.name} — the point is the reference town")
        in_town = [c for c in candidates if c.municipality == top.name]
        addresses = [c for c in in_town if c.kind == "address"]
        districts = [c for c in in_town if c.kind == "district" and c.district]
        postal = next((c.postal_code for c in in_town if c.postal_code), None)

        queries: list[GeocodeQuery] = []
        if addresses:
            best = max(addresses, key=lambda c: (c.house_number is not None, c.score))
            precision = GeocodePrecision.ADDRESS if best.house_number else GeocodePrecision.STREET
            queries.append(
                GeocodeQuery(
                    precision, top.name, street=best.street, house_number=best.house_number, postal_code=postal
                )
            )
            if len({a.street for a in addresses}) > 1:
                notes.append(f"{len({a.street for a in addresses})} street names mentioned; used '{best.street}'")
        if districts:
            queries.append(GeocodeQuery(GeocodePrecision.DISTRICT, top.name, district=districts[0].district))
        queries.append(GeocodeQuery(GeocodePrecision.MUNICIPALITY, top.name, postal_code=postal))

        base = LocationResolution(
            status=GeocodeStatus.UNRESOLVED,
            municipality=top.name,
            district=districts[0].district if districts else None,
            street=" ".join(p for p in (queries[0].street, queries[0].house_number) if p) or None,
            postal_code=postal,
            location_text=queries[0].label,
            candidates=trail,
            notes=notes,
        )
        return self._geocode(base, queries, (top.lat, top.lon))

    # -- geocode + validate ------------------------------------------------------------------
    def _geocode(
        self, base: LocationResolution, queries: list[GeocodeQuery], anchor: tuple[float, float]
    ) -> LocationResolution:
        rejected: list[str] = []
        for query in queries:
            for geocoder in self.geocoders:
                try:
                    hit = geocoder.geocode(query)
                except GeocoderUnavailable as exc:
                    self.provider_failures.append(f"{geocoder.name}: {exc}")
                    base.notes.append(f"{geocoder.name} unavailable for '{query.label}': {exc}")
                    continue
                if hit is None:
                    continue
                problem = self._validate(hit, anchor)
                if problem:
                    rejected.append(f"{geocoder.name} '{query.label}': {problem}")
                    continue
                x, y = project_25832(hit.lon, hit.lat)
                if _PRECISION_RANK.get(hit.precision, 9) > _PRECISION_RANK[queries[0].precision]:
                    base.notes.append(
                        f"precision downgraded from {queries[0].precision.value} to {hit.precision.value}"
                    )
                base.notes.extend(f"rejected {r}" for r in rejected)
                return base.model_copy(
                    update={
                        "status": GeocodeStatus.RESOLVED,
                        "precision": hit.precision,
                        "lon": round(hit.lon, 7),
                        "lat": round(hit.lat, 7),
                        "x_25832": x,
                        "y_25832": y,
                        "provider": hit.provider,
                        "provider_ref": hit.ref,
                        "confidence": round(min(1.0, hit.confidence), 3),
                        "location_text": query.label,
                    }
                )
        if rejected:
            base.notes.extend(f"rejected {r}" for r in rejected)
            return base.model_copy(update={"status": GeocodeStatus.INVALID})
        base.notes.append("no geocoder returned a result")
        return base

    def _validate(self, hit: GeoHit, anchor: tuple[float, float]) -> str | None:
        if hit.country_code and hit.country_code != "de":
            return f"country_code={hit.country_code}"
        try:
            project_25832(hit.lon, hit.lat)
        except InvalidCoordinate as exc:
            return str(exc)
        offset = distance_km(anchor, (hit.lat, hit.lon))
        if offset > self.max_offset_km:
            return f"{offset:.1f} km away from the expected municipality"
        return None
