"""HTML → :class:`Article` extraction for JLL newsroom / research pages.

Extraction priority (most → least authoritative):

* title          JSON-LD ``NewsArticle.headline`` → ``og:title`` → ``<h1>`` → ``<title>``
* published_at   JSON-LD ``datePublished`` if it agrees with the dateline (±2 days); otherwise the
                 dateline date (the release's own issue date) and a ``date_mismatch`` warning
* taxonomy       ``<meta name="ch.tagcloud">`` (JLL's own content taxonomy)
* body           ``.editorial__body`` blocks, minus the "Über JLL"/contact boilerplate

Missing or contradictory fields never raise silently: they are recorded in ``parse_warnings``.
A page with no usable title or body raises :class:`ParseError`.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .models import Article

BERLIN = ZoneInfo("Europe/Berlin")
MONTHS_DE = {
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}  # fmt: skip
_BOILERPLATE_HEADINGS = re.compile(r"^(über jll|about jll|kontakt|pressekontakt|contact)\b", re.I)
_BOILERPLATE_PARAGRAPH = re.compile(r"^JLL \(NYSE:\s*JLL\)", re.I)

_DATELINE = re.compile(
    r"""^\s*
    (?P<place>[A-ZÄÖÜ][\wÄÖÜäöüß.\-/() ]{1,60}?)\s*,\s*
    (?:
        (?P<day>\d{1,2})\.\s*(?P<month_name>[A-Za-zÄÖÜäöü]+)\s+(?P<year>\d{4})
      | (?P<nday>\d{1,2})\.\s*(?P<nmonth>\d{1,2})\.\s*(?P<nyear>\d{4})
    )
    \s*[–—-]\s*""",
    re.X,
)
_DATE_MISMATCH_TOLERANCE_DAYS = 2


class ParseError(Exception):
    def __init__(self, url: str, kind: str, message: str):
        super().__init__(f"{kind}: {message} ({url})")
        self.url = url
        self.kind = kind


def source_record_id(url: str) -> str:
    """Stable natural key: the lower-cased path below the locale, e.g. ``newsroom/<slug>``."""
    path = urlsplit(url).path.strip("/").lower()
    parts = path.split("/", 1)
    return parts[1] if len(parts) == 2 and re.fullmatch(r"[a-z]{2}-[a-z]{2,6}", parts[0]) else path


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    node = soup.find("meta", attrs={"name": name}) if name else soup.find("meta", attrs={"property": prop or ""})
    if isinstance(node, Tag):
        content = node.get("content")
        if isinstance(content, str) and content.strip():
            return _clean(content)
    return None


def _json_ld_objects(soup: BeautifulSoup) -> tuple[list[dict[str, Any]], list[str]]:
    objects: list[dict[str, Any]] = []
    warnings: list[str] = []
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = node.string or node.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            warnings.append("json_ld_malformed")
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack:
            if isinstance(item, dict):
                objects.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    objects.extend(g for g in graph if isinstance(g, dict))
    return objects, warnings


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BERLIN)


def parse_dateline(paragraph: str) -> tuple[str | None, date | None]:
    """``"MÖNCHENGLADBACH, 16. Juli 2026 – …"`` → ``("MÖNCHENGLADBACH", date(2026, 7, 16))``."""
    m = _DATELINE.match(paragraph)
    if not m:
        return None, None
    place = m.group("place").strip()
    try:
        if m.group("day"):
            month = MONTHS_DE.get(m.group("month_name").lower())
            if month is None:
                return place, None
            return place, date(int(m.group("year")), month, int(m.group("day")))
        return place, date(int(m.group("nyear")), int(m.group("nmonth")), int(m.group("nday")))
    except ValueError:
        return place, None


def _body_paragraphs(soup: BeautifulSoup) -> list[str]:
    blocks = soup.select(".editorial__body") or soup.select("article") or soup.select("main")
    paragraphs: list[str] = []
    for block in blocks:
        heading = block.find(["h2", "h3", "h4"])
        if isinstance(heading, Tag) and _BOILERPLATE_HEADINGS.match(_clean(heading.get_text(" "))):
            continue
        for el in block.find_all(["p", "li", "h2", "h3"]):
            if el.find_parent(["li"]) is not None and el.name == "p":
                continue  # nested paragraph inside a list item — the <li> text covers it
            text = _clean(el.get_text(" "))
            if len(text) < 3 or _BOILERPLATE_PARAGRAPH.match(text):
                continue
            if paragraphs and paragraphs[-1] == text:
                continue
            paragraphs.append(text)
    return paragraphs


def parse_article(html: str, url: str) -> Article:
    if not html or not html.strip():
        raise ParseError(url, "empty_document", "document is empty")
    soup = BeautifulSoup(html, "lxml")
    warnings: list[str] = []

    ld_objects, ld_warnings = _json_ld_objects(soup)
    warnings.extend(ld_warnings)
    news = next(
        (o for o in ld_objects if str(o.get("@type", "")).lower() in {"newsarticle", "article", "report"}),
        None,
    )
    if news is None:
        warnings.append("json_ld_article_missing")

    # --- title ---------------------------------------------------------------------------
    h1 = soup.find("h1")
    title_tag = soup.find("title")
    title = _clean(
        (news or {}).get("headline")
        or _meta(soup, prop="og:title")
        or (h1.get_text(" ") if isinstance(h1, Tag) else None)
        or (title_tag.get_text(" ") if isinstance(title_tag, Tag) else None)
    )
    if not title:
        raise ParseError(url, "missing_title", "no headline, og:title, <h1> or <title>")

    # --- canonical / language -------------------------------------------------------------
    canonical_node = soup.find("link", rel="canonical")
    canonical = (
        (canonical_node.get("href") if isinstance(canonical_node, Tag) else None) or _meta(soup, prop="og:url") or url
    )
    html_node = soup.find("html")
    language = (news or {}).get("inLanguage") or (html_node.get("lang") if isinstance(html_node, Tag) else None)

    # --- body & dateline -----------------------------------------------------------------
    paragraphs = _body_paragraphs(soup)
    if not paragraphs:
        raise ParseError(url, "missing_body", "no editorial body paragraphs found")
    dateline_place, dateline_date = parse_dateline(paragraphs[0])
    if dateline_place is None:
        warnings.append("dateline_missing")

    # --- publication date (verified, never assumed) --------------------------------------
    published_at = _parse_iso((news or {}).get("datePublished"))
    published_source: str | None = "json-ld" if published_at else None
    if published_at and dateline_date:
        delta = abs((published_at.astimezone(BERLIN).date() - dateline_date).days)
        if delta > _DATE_MISMATCH_TOLERANCE_DAYS:
            # Observed on jll.com: JSON-LD carries the CMS (re)publication date — often exactly 91 or
            # 365 days off — while the dateline is the date the release was issued. Trust the dateline.
            warnings.append(f"date_mismatch_jsonld_vs_dateline_{delta}d")
            warnings.append(f"jsonld_date_published:{published_at.isoformat()}")
            published_at = None
    if published_at is None and dateline_date is not None:
        published_at = datetime.combine(dateline_date, time(0, 0), tzinfo=BERLIN).astimezone(UTC)
        published_source = "dateline"
    if published_at is None:
        warnings.append("published_at_missing")
    elif published_at > datetime.now(UTC).replace(microsecond=0) and published_source == "json-ld":
        warnings.append("published_at_in_future")

    # --- taxonomy / keywords / misc -------------------------------------------------------
    tagcloud = _meta(soup, name="ch.tagcloud") or ""
    taxonomy = sorted({t.strip().removeprefix("jll-taxonomy:") for t in tagcloud.split(",") if t.strip()})
    if not taxonomy:
        warnings.append("taxonomy_missing")
    keywords = [k.strip() for k in (_meta(soup, name="keywords") or "").split(",") if k.strip()]
    authors = [
        _clean(a.get("name")) for a in ((news or {}).get("author") or []) if isinstance(a, dict) and a.get("name")
    ]
    image = (news or {}).get("image") or _meta(soup, prop="og:image")

    return Article(
        source_record_id=source_record_id(str(canonical or url)),
        url=url,
        canonical_url=str(canonical),
        title=title,
        subtitle=_meta(soup, name="description"),
        published_at=published_at,
        published_at_source=published_source,
        dateline_place=dateline_place,
        dateline_date=dateline_date,
        language=str(language) if language else None,
        authors=authors,
        keywords=keywords,
        taxonomy=taxonomy,
        image_url=str(image) if isinstance(image, str) else None,
        body_text="\n\n".join(paragraphs),
        paragraphs=paragraphs,
        parse_warnings=warnings,
    )
