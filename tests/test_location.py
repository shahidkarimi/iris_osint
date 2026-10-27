from __future__ import annotations

import math
from typing import Any

import httpx
import pytest

from jll_harvester.gazetteer import Gazetteer, extract_candidates, fold
from jll_harvester.geocode import (
    GazetteerGeocoder,
    GeocodeQuery,
    GeoHit,
    InvalidCoordinate,
    LocationResolver,
    NominatimGeocoder,
    project_25832,
)
from jll_harvester.models import GeocodePrecision, GeocodeStatus
from jll_harvester.parsing import parse_article

from .conftest import load_article


# ---------------------------------------------------------------------------------------------
# Gazetteer
# ---------------------------------------------------------------------------------------------
def test_fold_handles_umlauts() -> None:
    assert fold("Mönchengladbach") == fold("Moenchengladbach") == "moenchengladbach"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("München", "München"),
        ("Köln", "Köln"),
        ("Frankfurt", "Frankfurt am Main"),
        ("Sassenburg", "Sassenburg"),
        ("Dombühl", "Dombühl"),
    ],
)
def test_place_lookup(gazetteer: Gazetteer, name: str, expected: str) -> None:
    assert gazetteer.places(name)[0].name == expected


def test_regions_vs_places(gazetteer: Gazetteer) -> None:
    assert gazetteer.is_region("Oberbayern")
    assert gazetteer.is_region("Mittelfranken")
    assert not gazetteer.is_region("Köln")  # Regierungsbezirk Köln exists, but the city wins
    assert not gazetteer.is_region("Berlin")


def test_district_lookup_is_proximity_checked(gazetteer: Gazetteer) -> None:
    bonn = gazetteer.places("Bonn")[0]
    assert gazetteer.districts("Auerberg", near=(bonn.lat, bonn.lon))
    hamburg = gazetteer.places("Hamburg")[0]
    assert not gazetteer.districts("Auerberg", near=(hamburg.lat, hamburg.lon))


def test_demonyms(gazetteer: Gazetteer) -> None:
    assert gazetteer.demonym_to_place("Münchner") == "München"
    assert gazetteer.demonym_to_place("Kölner") == "Köln"
    assert gazetteer.demonym_to_place("Aachener") == "Aachen"


def _article(title: str, body: str) -> Any:
    return parse_article(
        f"<html><head><title>{title}</title></head><body><div class='editorial__body'><p>{body}</p></div></body></html>",
        "https://www.jll.com/de-de/newsroom/x",
    )


def test_common_words_are_not_places(gazetteer: Gazetteer) -> None:
    a = _article("Bürofläche in zentraler Lage", "Die Halle in der Mitte des Areals hat eine gute Lage.")
    assert [c for c in extract_candidates(a, gazetteer) if c.kind == "place"] == []


def test_street_attaches_to_nearest_preceding_town(gazetteer: Gazetteer) -> None:
    a = _article(
        "Projektsteuerung für Kosmetikhersteller",
        "AACHEN, 19. November 2024 – Das Unternehmen hat seinen Aachener Standort in der Neuenhofstraße 180 erweitert. "
        "Ein Lager befindet sich im benachbarten Eschweiler.",
    )
    streets = [c for c in extract_candidates(a, gazetteer) if c.kind == "address"]
    assert streets and streets[0].municipality == "Aachen" and streets[0].house_number == "180"


# ---------------------------------------------------------------------------------------------
# Projection & validation
# ---------------------------------------------------------------------------------------------
def test_projection_to_25832_matches_reference() -> None:
    # Cologne cathedral: lon 6.9583, lat 50.9413 → UTM 32N approx. (356 400, 5 645 000)
    x, y = project_25832(6.9583, 50.9413)
    assert 356_000 < x < 357_000 and 5_644_500 < y < 5_645_500


@pytest.mark.parametrize(
    ("lon", "lat"),
    [(float("nan"), 50.0), (math.inf, 50.0), (2.35, 48.85), (13.4, 70.0), (200.0, 50.0), (16.37, 48.21)],
)
def test_invalid_coordinates_are_rejected(lon: float, lat: float) -> None:
    with pytest.raises(InvalidCoordinate):
        project_25832(lon, lat)


# ---------------------------------------------------------------------------------------------
# Resolver (offline gazetteer)
# ---------------------------------------------------------------------------------------------
def test_district_level_resolution(offline_resolver: LocationResolver) -> None:
    loc = offline_resolver.resolve(load_article("Univers-Holding-kauft-und-entwickelt-Brownfield-in-Bonn-Auerberg"))
    assert loc.status == GeocodeStatus.RESOLVED
    assert loc.precision == GeocodePrecision.DISTRICT
    assert (loc.municipality, loc.district) == ("Bonn", "Auerberg")
    assert loc.x_25832 is not None and 300_000 < loc.x_25832 < 400_000


def test_facility_name_beats_weak_mentions(offline_resolver: LocationResolver) -> None:
    loc = offline_resolver.resolve(load_article("Logistiker-mietet-rund-46-000-m2-Hallenflaeche-in-Mittelfranken"))
    assert loc.municipality == "Dombühl"


def test_multi_site_portfolio_is_ambiguous_without_point(offline_resolver: LocationResolver) -> None:
    loc = offline_resolver.resolve(load_article("eqt-kauft-logistikportfolio-southgates-mit-insgesamt-50-000-m"))
    assert loc.status == GeocodeStatus.AMBIGUOUS
    assert loc.x_25832 is None and loc.lon is None
    assert loc.candidates and loc.notes


def test_region_only_stays_unresolved(offline_resolver: LocationResolver) -> None:
    loc = offline_resolver.resolve(load_article("synthetic-region-only"))
    assert loc.status == GeocodeStatus.UNRESOLVED
    assert loc.x_25832 is None
    assert "region-level" in loc.notes[0]


def test_offline_downgrades_street_to_coarser_precision(offline_resolver: LocationResolver) -> None:
    loc = offline_resolver.resolve(load_article("realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach"))
    assert loc.status == GeocodeStatus.RESOLVED
    assert loc.municipality == "Mönchengladbach" and loc.street == "Regioparkring 4"
    assert loc.precision == GeocodePrecision.MUNICIPALITY
    assert any("downgraded" in n for n in loc.notes)


# ---------------------------------------------------------------------------------------------
# Nominatim (mocked) + validation of provider answers
# ---------------------------------------------------------------------------------------------
def _nominatim(handler: Any, cache: dict[str, Any] | None = None) -> NominatimGeocoder:
    return NominatimGeocoder(
        base_url="https://nominatim.test",
        user_agent="test",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
        cache=cache,
    )


def test_nominatim_address_hit_is_used_and_cached(gazetteer: Gazetteer) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "51.1647",
                    "lon": "6.3803",
                    "osm_type": "way",
                    "osm_id": 1,
                    "importance": 0.4,
                    "display_name": "Regioparkring 4, Mönchengladbach",
                    "address": {"house_number": "4", "country_code": "de"},
                }
            ],
        )

    cache: dict[str, Any] = {}
    resolver = LocationResolver(gazetteer, [_nominatim(handler, cache), GazetteerGeocoder(gazetteer)])
    article = load_article("realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach")
    loc = resolver.resolve(article)
    assert loc.status == GeocodeStatus.RESOLVED and loc.precision == GeocodePrecision.ADDRESS
    assert loc.provider == "nominatim" and loc.provider_ref == "osm:way/1"
    assert calls[0].url.params["street"] == "4 Regioparkring"
    assert calls[0].url.params["city"] == "Mönchengladbach"
    resolver.resolve(article)
    assert len(calls) == 1  # second resolution served from cache


def test_far_away_provider_answer_is_rejected_then_fallback(gazetteer: Gazetteer) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # a same-named street in Hamburg
        return httpx.Response(
            200, json=[{"lat": "53.55", "lon": "10.0", "address": {"house_number": "4", "country_code": "de"}}]
        )

    resolver = LocationResolver(gazetteer, [_nominatim(handler), GazetteerGeocoder(gazetteer)])
    loc = resolver.resolve(load_article("realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach"))
    assert loc.status == GeocodeStatus.RESOLVED and loc.provider == "geonames-offline"
    assert any("km away" in n for n in loc.notes)


def test_only_invalid_answers_mark_record_invalid(gazetteer: Gazetteer) -> None:
    class Austria:
        name = "bad"

        def geocode(self, query: GeocodeQuery) -> GeoHit:
            return GeoHit(48.21, 16.37, query.precision, "bad", country_code="at")

    loc = LocationResolver(gazetteer, [Austria()]).resolve(
        load_article("henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg")
    )
    assert loc.status == GeocodeStatus.INVALID
    assert loc.x_25832 is None
    assert any("country_code=at" in n for n in loc.notes)


def test_provider_outage_falls_back_and_is_recorded(gazetteer: Gazetteer) -> None:
    resolver = LocationResolver(gazetteer, [_nominatim(lambda r: httpx.Response(503)), GazetteerGeocoder(gazetteer)])
    loc = resolver.resolve(load_article("henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg"))
    assert loc.status == GeocodeStatus.RESOLVED and loc.provider == "geonames-offline"
    assert resolver.provider_failures
