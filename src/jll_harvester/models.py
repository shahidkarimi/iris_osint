"""Domain models shared by the pipeline stages.

The flow is::

    SitemapEntry ──fetch──▶ RawDocument ──parse──▶ Article ──classify──▶ Classification
                                                      │
                                                      └─locate──▶ LocationResolution
                                                                      │
                                       Article + Classification + LocationResolution ──▶ Signal
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SignalType(StrEnum):
    COMMERCIAL_LAND_DISPOSAL = "commercial_land_disposal"
    BIG_BOX_INDUSTRIAL_DEVELOPMENT = "big_box_industrial_development"


class GeocodeStatus(StrEnum):
    RESOLVED = "resolved"  # a validated EPSG:25832 point exists
    AMBIGUOUS = "ambiguous"  # several plausible locations; no point is asserted
    UNRESOLVED = "unresolved"  # no location precise enough for a point (e.g. region only)
    INVALID = "invalid"  # a coordinate was produced but failed validation and was rejected


class GeocodePrecision(StrEnum):
    ADDRESS = "address"
    STREET = "street"
    DISTRICT = "district"
    MUNICIPALITY = "municipality"
    REGION = "region"
    COUNTRY = "country"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SitemapEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    lastmod: datetime | None = None


class RawDocument(BaseModel):
    url: str
    final_url: str
    status_code: int
    content: str
    sha256: str
    fetched_at: datetime
    content_type: str | None = None


class Article(BaseModel):
    """A parsed press release / research note. Nothing here is inferred — only extracted."""

    source_record_id: str
    url: str
    canonical_url: str
    title: str
    subtitle: str | None = None
    published_at: datetime | None = None
    published_at_source: str | None = None  # "json-ld" | "dateline"
    dateline_place: str | None = None
    dateline_date: date | None = None
    language: str | None = None
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    taxonomy: list[str] = Field(default_factory=list)
    image_url: str | None = None
    body_text: str = ""
    paragraphs: list[str] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)

    @property
    def news_categories(self) -> set[str]:
        return {t.split("/", 1)[1] for t in self.taxonomy if t.startswith("news/")}

    @property
    def property_types(self) -> set[str]:
        return {t.split("/", 1)[1] for t in self.taxonomy if t.startswith("property-types/")}

    @property
    def taxonomy_locations(self) -> list[str]:
        return [t.removeprefix("location/") for t in self.taxonomy if t.startswith("location/")]


class Classification(BaseModel):
    signal_type: SignalType | None
    confidence: float
    deal_type: str | None = None
    area_sqm: float | None = None
    plot_area_sqm: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    rejection_reason: str | None = None

    @property
    def is_signal(self) -> bool:
        return self.signal_type is not None


class LocationCandidate(BaseModel):
    """A place mention found in the text, before any coordinates are attached."""

    text: str
    kind: str  # "address" | "district" | "place" | "region"
    origin: str  # "title" | "body" | "dateline" | "address" | "demonym"
    municipality: str | None = None
    district: str | None = None
    street: str | None = None
    house_number: str | None = None
    postal_code: str | None = None
    score: float = 0.0


class LocationResolution(BaseModel):
    status: GeocodeStatus
    precision: GeocodePrecision | None = None
    location_text: str | None = None
    municipality: str | None = None
    district: str | None = None
    street: str | None = None
    postal_code: str | None = None
    country_code: str = "DE"
    lon: float | None = None
    lat: float | None = None
    x_25832: float | None = None
    y_25832: float | None = None
    provider: str | None = None
    provider_ref: str | None = None
    confidence: float | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Signal(BaseModel):
    """The normalised record that lands in ``iris_staging.signal_ingest``."""

    task_id: str
    source_system: str
    source_record_id: str
    source_url: str
    canonical_url: str
    signal_type: SignalType
    title: str
    summary: str | None
    published_at: datetime | None
    published_at_source: str | None
    source_lastmod: datetime | None
    language: str | None
    deal_type: str | None
    area_sqm: float | None
    plot_area_sqm: float | None
    property_types: list[str]
    source_tags: list[str]
    classification_confidence: float
    classification_evidence: dict[str, Any]
    location: LocationResolution
    quality_flags: list[str]
    raw_sha256: str | None
    content_hash: str
    payload: dict[str, Any]


class Issue(BaseModel):
    severity: IssueSeverity
    stage: str
    code: str
    message: str
    source_url: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
