"""Source discovery via the robots.txt-advertised XML sitemap.

Why the sitemap and not the listing page?

* The brief's endpoint (``jll.de/de/trends-and-insights/research``) answers ``301`` and lands on
  ``jll.com/de-de/insights/market-dynamics``, whose teaser grid is rendered client-side.
* The listing's "load more" pagination uses query strings, which robots.txt forbids (``Disallow: /*?``).
* ``https://www.jll.com/de-de/sitemap.xml`` is linked from robots.txt, lists every newsroom and
  research URL, and carries ``<lastmod>`` — giving lawful pagination-free enumeration *and*
  incremental retrieval.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

from lxml import etree

from .http import FetchError, PoliteClient
from .models import SitemapEntry

log = logging.getLogger(__name__)

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_MAX_INDEX_DEPTH = 2


class DiscoveryError(Exception):
    def __init__(self, url: str, kind: str, message: str):
        super().__init__(f"{kind}: {message} ({url})")
        self.url = url
        self.kind = kind


def parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_sitemap(xml: bytes | str, url: str = "<memory>") -> tuple[list[SitemapEntry], list[str]]:
    """Return ``(url entries, child sitemap urls)``. Raises :class:`DiscoveryError` on bad XML."""
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    if not data.strip():
        raise DiscoveryError(url, "empty_sitemap", "sitemap body is empty")
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise DiscoveryError(url, "malformed_sitemap", str(exc)) from exc

    tag = etree.QName(root).localname
    entries: list[SitemapEntry] = []
    children: list[str] = []
    if tag == "sitemapindex":
        for node in root.iter(f"{_NS}sitemap", "sitemap"):
            loc = node.findtext(f"{_NS}loc") or node.findtext("loc")
            if loc and loc.strip():
                children.append(loc.strip())
    elif tag == "urlset":
        for node in root.iter(f"{_NS}url", "url"):
            loc = node.findtext(f"{_NS}loc") or node.findtext("loc")
            if not loc or not loc.strip():
                continue
            lastmod = node.findtext(f"{_NS}lastmod") or node.findtext("lastmod")
            entries.append(SitemapEntry(url=loc.strip(), lastmod=parse_lastmod(lastmod)))
    else:
        raise DiscoveryError(url, "malformed_sitemap", f"unexpected root element <{tag}>")
    return entries, children


def filter_section(entries: list[SitemapEntry], prefixes: list[str]) -> list[SitemapEntry]:
    """Keep article URLs below the configured section prefixes (not the section index pages)."""
    seen: set[str] = set()
    kept: list[SitemapEntry] = []
    for entry in entries:
        path = urlsplit(entry.url).path
        if urlsplit(entry.url).query:
            continue
        if not any(path.startswith(p) and len(path) > len(p) for p in prefixes):
            continue
        key = path.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return kept


def discover(client: PoliteClient, sitemap_url: str, prefixes: list[str]) -> list[SitemapEntry]:
    """Enumerate candidate article URLs, newest ``lastmod`` first."""
    pending: list[tuple[str, int]] = [(sitemap_url, 0)]
    collected: list[SitemapEntry] = []
    while pending:
        url, depth = pending.pop()
        try:
            doc = client.fetch(url)
        except FetchError as exc:
            raise DiscoveryError(url, exc.kind, exc.detail) from exc
        entries, children = parse_sitemap(doc.content, url)
        collected.extend(entries)
        if children and depth >= _MAX_INDEX_DEPTH:
            log.warning("sitemap index nesting too deep; skipping children", extra={"url": url})
            continue
        pending.extend((child, depth + 1) for child in children)

    selected = filter_section(collected, prefixes)
    epoch = datetime.min.replace(tzinfo=UTC)
    selected.sort(key=lambda e: e.lastmod or epoch, reverse=True)
    log.info("sitemap discovery finished", extra={"total": len(collected), "selected": len(selected)})
    return selected
