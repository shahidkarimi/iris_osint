from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from jll_harvester.discovery import DiscoveryError, discover, filter_section, parse_lastmod, parse_sitemap
from jll_harvester.http import FetchError, PoliteClient
from jll_harvester.models import SitemapEntry

ROBOTS = "User-agent: *\nDisallow: /*?\nSitemap: https://www.jll.com/sitemap-index.xml\n"


def make_client(
    handler: Callable[[httpx.Request], httpx.Response], sleeps: list[float] | None = None, **kw: object
) -> PoliteClient:
    record = sleeps if sleeps is not None else []
    return PoliteClient(
        user_agent="test-bot/1.0",
        delay_seconds=0,
        backoff_base_seconds=0.01,
        transport=httpx.MockTransport(handler),
        sleep=record.append,
        **kw,  # type: ignore[arg-type]
    )


def routes(table: dict[str, list[httpx.Response] | httpx.Response]) -> Callable[[httpx.Request], httpx.Response]:
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        spec = table.get(key)
        if spec is None:
            return httpx.Response(404, text="not found")
        if isinstance(spec, list):
            i = calls.get(key, 0)
            calls[key] = i + 1
            return spec[min(i, len(spec) - 1)]
        return spec

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


# ---------------------------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------------------------
def test_retries_transient_errors_then_succeeds() -> None:
    handler = routes(
        {
            "/robots.txt": httpx.Response(200, text=ROBOTS),
            "/de-de/newsroom/a": [
                httpx.Response(503),
                httpx.Response(502),
                httpx.Response(200, text="<html>ok</html>"),
            ],
        }
    )
    sleeps: list[float] = []
    with make_client(handler, sleeps) as client:
        doc = client.fetch("https://www.jll.com/de-de/newsroom/a")
    assert doc.status_code == 200 and "ok" in doc.content
    assert len(doc.sha256) == 64
    assert len(sleeps) == 2  # two backoff waits


def test_retry_after_header_is_honoured() -> None:
    handler = routes(
        {
            "/robots.txt": httpx.Response(200, text=ROBOTS),
            "/x": [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="fine")],
        }
    )
    sleeps: list[float] = []
    with make_client(handler, sleeps) as client:
        client.fetch("https://www.jll.com/x")
    assert max(sleeps) >= 7


def test_not_found_is_not_retried() -> None:
    handler = routes({"/robots.txt": httpx.Response(200, text=ROBOTS)})
    sleeps: list[float] = []
    with make_client(handler, sleeps) as client, pytest.raises(FetchError) as exc:
        client.fetch("https://www.jll.com/gone")
    assert exc.value.kind == "not_found"
    assert sleeps == []


def test_gives_up_after_max_retries() -> None:
    handler = routes({"/robots.txt": httpx.Response(200, text=ROBOTS), "/flaky": httpx.Response(500)})
    with make_client(handler, max_retries=2) as client, pytest.raises(FetchError) as exc:
        client.fetch("https://www.jll.com/flaky")
    assert exc.value.kind == "http_500"


def test_network_errors_are_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        raise httpx.ConnectError("connection refused", request=request)

    with make_client(handler, max_retries=1) as client, pytest.raises(FetchError) as exc:
        client.fetch("https://www.jll.com/a")
    assert exc.value.kind == "network_error"


def test_robots_disallowed_url_is_never_requested() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=ROBOTS if request.url.path == "/robots.txt" else "x")

    with make_client(handler) as client, pytest.raises(FetchError) as exc:
        client.fetch("https://www.jll.com/de-de/newsroom?page=2")
    assert exc.value.kind == "robots_disallowed"
    assert seen == ["https://www.jll.com/robots.txt"]


def test_empty_body_is_an_error() -> None:
    handler = routes({"/robots.txt": httpx.Response(200, text=ROBOTS), "/empty": httpx.Response(200, text="  \n")})
    with make_client(handler) as client, pytest.raises(FetchError) as exc:
        client.fetch("https://www.jll.com/empty")
    assert exc.value.kind == "empty_response"


def test_missing_robots_means_no_restrictions() -> None:
    handler = routes({"/page": httpx.Response(200, text="hello")})
    with make_client(handler) as client:
        assert client.fetch("https://www.jll.com/page").content == "hello"


# ---------------------------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------------------------
URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.jll.com/de-de/newsroom/b-artikel</loc><lastmod>2026-10-09T09:00:00.000Z</lastmod></url>
  <url><loc>https://www.jll.com/de-de/newsroom/A-Artikel</loc><lastmod>2026-07-16T06:00:00Z</lastmod></url>
  <url><loc>https://www.jll.com/de-de/newsroom/a-artikel</loc></url>
  <url><loc>https://www.jll.com/de-de/newsroom</loc></url>
  <url><loc>https://www.jll.com/de-de/newsroom?page=3</loc></url>
  <url><loc>https://www.jll.com/de-de/services/leasing</loc></url>
  <url><loc>  </loc></url>
</urlset>"""
INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.jll.com/de-de/sitemap.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_urlset_and_lastmod() -> None:
    entries, children = parse_sitemap(URLSET)
    assert children == []
    assert len(entries) == 6
    assert entries[0].lastmod is not None and entries[0].lastmod.tzinfo is not None
    assert parse_lastmod("not-a-date") is None


def test_parse_sitemap_index() -> None:
    entries, children = parse_sitemap(INDEX)
    assert entries == [] and children == ["https://www.jll.com/de-de/sitemap.xml"]


@pytest.mark.parametrize(
    ("body", "kind"), [("", "empty_sitemap"), ("<urlset><url>", "malformed_sitemap"), ("<html/>", "malformed_sitemap")]
)
def test_bad_sitemaps_raise(body: str, kind: str) -> None:
    with pytest.raises(DiscoveryError) as exc:
        parse_sitemap(body)
    assert exc.value.kind == kind


def test_xml_entities_are_not_expanded() -> None:
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&e;</loc></url></urlset>'
    entries, _ = parse_sitemap(evil)
    assert all("root:" not in e.url for e in entries)


def test_filter_section_dedupes_and_drops_index_and_query_urls() -> None:
    entries, _ = parse_sitemap(URLSET)
    kept = filter_section(entries, ["/de-de/newsroom/"])
    assert [e.url.rsplit("/", 1)[-1] for e in kept] == ["b-artikel", "A-Artikel"]


def test_discover_follows_index_and_sorts_newest_first() -> None:
    handler = routes(
        {
            "/robots.txt": httpx.Response(200, text=ROBOTS),
            "/sitemap-index.xml": httpx.Response(200, text=INDEX),
            "/de-de/sitemap.xml": httpx.Response(200, text=URLSET),
        }
    )
    with make_client(handler) as client:
        found = discover(client, "https://www.jll.com/sitemap-index.xml", ["/de-de/newsroom/"])
    assert [e.url.rsplit("/", 1)[-1] for e in found] == ["b-artikel", "A-Artikel"]
    assert isinstance(found[0], SitemapEntry)


def test_discover_wraps_fetch_failures() -> None:
    handler = routes({"/robots.txt": httpx.Response(200, text=ROBOTS), "/de-de/sitemap.xml": httpx.Response(503)})
    with make_client(handler, max_retries=0) as client, pytest.raises(DiscoveryError) as exc:
        discover(client, "https://www.jll.com/de-de/sitemap.xml", ["/de-de/newsroom/"])
    assert exc.value.kind == "http_503"


def test_access_denied_is_reported_not_bypassed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["user-agent"])
        return httpx.Response(403, headers={"server": "AkamaiGHost"}, text="Access Denied")

    with make_client(handler) as client, pytest.raises(FetchError) as exc:
        client.fetch("https://www.jll.com/de-de/sitemap.xml")
    assert exc.value.kind == "access_denied"
    assert "AkamaiGHost" in str(exc.value)
    assert set(calls) == {"test-bot/1.0"}  # same identity every time: no UA rotation, no retries
    assert len(calls) == 2  # robots.txt + the page, each exactly once
