"""Assemble the staging record, derive quality flags and the idempotency hash."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from .models import (
    Article,
    Classification,
    GeocodePrecision,
    GeocodeStatus,
    LocationResolution,
    Signal,
)

# Fields that define "the same record". Fetch timestamps, run ids and the raw HTML hash are
# deliberately excluded so that re-harvesting an unchanged article is a no-op.
_HASHED_FIELDS = (
    "signal_type", "title", "summary", "published_at", "published_at_source", "deal_type",
    "area_sqm", "plot_area_sqm", "property_types", "source_tags", "classification_confidence",
    "location", "canonical_url",
)  # fmt: skip
_EXCERPT_CHARS = 1200


def content_hash(record: dict[str, Any]) -> str:
    material = {k: record.get(k) for k in _HASHED_FIELDS}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def quality_flags(article: Article, cls: Classification, loc: LocationResolution) -> list[str]:
    flags: list[str] = []
    if article.published_at is None:
        flags.append("published_at_missing")
    elif article.published_at_source == "dateline":
        flags.append("published_at_from_dateline")
    flags.extend(
        w
        for w in article.parse_warnings
        if w.startswith(("date_mismatch", "published_at_in_future", "json_ld_malformed"))
    )
    if loc.status != GeocodeStatus.RESOLVED:
        flags.append(f"location_{loc.status.value}")
    elif loc.precision == GeocodePrecision.MUNICIPALITY:
        flags.append("location_coarse_municipality")
    if any("dateline only" in n for n in loc.notes):
        flags.append("location_from_dateline_only")
    if any("only described as near" in n for n in loc.notes):
        flags.append("location_near_reference_town")
    if any(n.startswith("precision downgraded") for n in loc.notes):
        flags.append("location_precision_downgraded")
    if cls.area_sqm is None and cls.plot_area_sqm is None:
        flags.append("area_not_stated")
    if cls.confidence < 0.65:
        flags.append("low_classification_confidence")
    if "taxonomy_missing" in article.parse_warnings:
        flags.append("taxonomy_missing")
    return sorted(set(flags))


def build_signal(
    *,
    article: Article,
    classification: Classification,
    location: LocationResolution,
    task_id: str,
    source_system: str,
    source_lastmod: datetime | None,
    raw_sha256: str | None,
) -> Signal:
    if classification.signal_type is None:
        raise ValueError("cannot build a signal from a rejected classification")
    flags = quality_flags(article, classification, location)
    record: dict[str, Any] = {
        "signal_type": classification.signal_type.value,
        "title": article.title,
        "summary": article.subtitle,
        "published_at": article.published_at.isoformat() if article.published_at else None,
        "published_at_source": article.published_at_source,
        "deal_type": classification.deal_type,
        "area_sqm": classification.area_sqm,
        "plot_area_sqm": classification.plot_area_sqm,
        "property_types": sorted(article.property_types),
        "source_tags": article.taxonomy,
        "classification_confidence": classification.confidence,
        "location": location.model_dump(mode="json", exclude={"candidates"}),
        "canonical_url": article.canonical_url,
    }
    payload = {
        "article": {
            "title": article.title,
            "subtitle": article.subtitle,
            "dateline_place": article.dateline_place,
            "dateline_date": article.dateline_date.isoformat() if article.dateline_date else None,
            "authors": article.authors,
            "keywords": article.keywords,
            "image_url": article.image_url,
            "lead": article.paragraphs[0] if article.paragraphs else None,
            "excerpt": article.body_text[:_EXCERPT_CHARS],
            "parse_warnings": article.parse_warnings,
        },
        "classification": classification.model_dump(mode="json"),
        "location": location.model_dump(mode="json"),
    }
    return Signal(
        task_id=task_id,
        source_system=source_system,
        source_record_id=article.source_record_id,
        source_url=article.url,
        canonical_url=article.canonical_url,
        signal_type=classification.signal_type,
        title=article.title,
        summary=article.subtitle,
        published_at=article.published_at,
        published_at_source=article.published_at_source,
        source_lastmod=source_lastmod,
        language=article.language,
        deal_type=classification.deal_type,
        area_sqm=classification.area_sqm,
        plot_area_sqm=classification.plot_area_sqm,
        property_types=sorted(article.property_types),
        source_tags=article.taxonomy,
        classification_confidence=classification.confidence,
        classification_evidence=classification.evidence,
        location=location,
        quality_flags=flags,
        raw_sha256=raw_sha256,
        content_hash=content_hash(record),
        payload=payload,
    )


def is_german(article: Article, location: LocationResolution) -> bool:
    """Geographic filter: the signal must concern a site in Germany."""
    if location.municipality or location.status == GeocodeStatus.AMBIGUOUS:
        return True  # municipality comes from the German gazetteer
    if any(loc.endswith("germany") or "/germany/" in loc for loc in article.taxonomy_locations):
        return True
    return any(c.get("kind") in {"place", "district", "address"} for c in location.candidates)
