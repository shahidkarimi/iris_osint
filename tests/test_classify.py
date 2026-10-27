from __future__ import annotations

import pytest

from jll_harvester.classify import SignalClassifier, extract_areas, parse_number_de
from jll_harvester.models import SignalType

from .conftest import load_article

LAND = SignalType.COMMERCIAL_LAND_DISPOSAL
BIGBOX = SignalType.BIG_BOX_INDUSTRIAL_DEVELOPMENT


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        # real (trimmed) JLL press releases
        ("realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach", BIGBOX),
        ("chinesischer-handelskonzern-mietet-41200m-logistikflaeche-bei-berlin", BIGBOX),
        ("Logistiker-mietet-rund-46-000-m2-Hallenflaeche-in-Mittelfranken", BIGBOX),
        ("logistikdienstleister-mietet-37300-m-nutzflaeche-im-segro-park-dortmund", BIGBOX),
        ("eqt-kauft-logistikportfolio-southgates-mit-insgesamt-50-000-m", BIGBOX),
        ("Univers-Holding-kauft-und-entwickelt-Brownfield-in-Bonn-Auerberg", LAND),
        ("universa-versicherung-kauft-grundstueck-in-nuernberg-fuer-neue-firmenzentrale", LAND),
        ("henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg", LAND),
        # synthetic
        ("synthetic-malformed-jsonld-dateline-only", LAND),
        ("synthetic-region-only", LAND),
    ],
)
def test_in_scope_signals(classifier: SignalClassifier, slug: str, expected: SignalType) -> None:
    result = classifier.classify(load_article(slug))
    assert result.signal_type == expected, result.evidence
    assert 0.5 <= result.confidence <= 1.0
    assert result.evidence["rules"][expected.value]  # every decision is explained


@pytest.mark.parametrize(
    ("slug", "reason"),
    [
        ("logistikinvestmentmarkt-bestaetigt-aufwaertstrend", "market_research"),
        ("Daniel-Siegl-wird-bei-JLL-neuer-Team-Leader-Industrial-Logistics-Sued", "not_a_single_deal"),
        ("jll-vermittelt-rund-3200-m2-bueroflaeche-am-europlatz-in-heidelberg", "out_of_domain:office"),
        (
            "Family-Office-verkauft-Wohnanlage-in-Berlin-an-Domicil-Real-Estate-AG",
            "out_of_domain:living-and-multifamily",
        ),
        ("bellaqa-gmbh-erwirbt-gewerbeimmobilie-von-sks-gmbh-beim-muenchner-flughafen", "below_confidence_threshold"),
        ("synthetic-wohnbaugrundstueck", "below_confidence_threshold"),
    ],
)
def test_out_of_scope_documents_are_rejected_with_reason(classifier: SignalClassifier, slug: str, reason: str) -> None:
    result = classifier.classify(load_article(slug))
    assert result.signal_type is None
    assert result.rejection_reason == reason


def test_residential_land_rule_fires(classifier: SignalClassifier) -> None:
    result = classifier.classify(load_article("synthetic-wohnbaugrundstueck"))
    assert "residential_land" in result.evidence["rules"]["commercial_land_disposal"]


def test_land_wins_when_named_in_title_even_if_bigbox_also_matches(classifier: SignalClassifier) -> None:
    result = classifier.classify(load_article("synthetic-malformed-jsonld-dateline-only"))
    assert result.signal_type == LAND
    assert result.plot_area_sqm == 80_000
    assert "big_box_industrial_development" in result.evidence.get("also_matches", [])


def test_letting_uses_let_area_not_building_size(classifier: SignalClassifier) -> None:
    result = classifier.classify(load_article("Logistiker-mietet-rund-46-000-m2-Hallenflaeche-in-Mittelfranken"))
    assert result.deal_type == "letting"
    assert result.area_sqm == 46_000


def test_big_box_threshold_is_configurable() -> None:
    strict = SignalClassifier(big_box_min_sqm=60_000)
    assert (
        strict.classify(
            load_article("chinesischer-handelskonzern-mietet-41200m-logistikflaeche-bei-berlin")
        ).signal_type
        is None
    )


@pytest.mark.parametrize(
    ("text", "value"),
    [("37.350", 37350), ("41.200", 41200), ("3,5", 3.5), ("50 000", 50000), ("10 500", 10500), ("12", 12)],
)
def test_parse_german_numbers(text: str, value: float) -> None:
    assert parse_number_de(text) == value


def test_extract_areas_kinds_and_units() -> None:
    text = (
        "Das Gebäude verfügt über eine Nutzfläche von mehr als 170.000 m², errichtet auf einem 110.000 m² großen "
        "Grundstück. Zusätzlich gehört ein 3,5 ha großes Areal dazu."
    )
    areas = extract_areas(text)
    assert [(a["sqm"], a["kind"]) for a in areas] == [(170_000, "building"), (110_000, "plot"), (35_000, "plot")]


def test_research_insight_pages_are_market_research(classifier: SignalClassifier) -> None:
    html = """<html><head><title>Berlin Büromarktbericht, Q2 2026</title>
      <meta name="ch.tagcloud" content="jll-taxonomy:content-type/marketing-article/insight,jll-taxonomy:property-types/office">
      </head><body><div class="editorial__body"><p>Der Berliner Büromarkt verzeichnete im zweiten Quartal …</p></div></body></html>"""
    from jll_harvester.parsing import parse_article

    result = classifier.classify(
        parse_article(html, "https://www.jll.com/de-de/insights/market-dynamics/berlin-office")
    )
    assert result.rejection_reason == "market_research"
