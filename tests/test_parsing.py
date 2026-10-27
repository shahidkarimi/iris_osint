from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from jll_harvester.parsing import ParseError, parse_article, parse_dateline, source_record_id

from .conftest import HTML, load_article


def test_real_press_release_is_fully_extracted() -> None:
    a = load_article("realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach")
    assert a.title == "Realterm kauft ehemaliges Zerres-Logistikzentrum in Mönchengladbach"
    assert a.published_at == datetime(2026, 7, 16, 6, 0, tzinfo=UTC)
    assert a.published_at_source == "json-ld"
    assert a.dateline_place == "MÖNCHENGLADBACH" and a.dateline_date == date(2026, 7, 16)
    assert a.source_record_id == "newsroom/realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach"
    assert "news/transactions" in a.taxonomy
    assert a.property_types == {"industrial-and-logistics"}
    assert a.authors == ["Julia Overdiek"]
    assert a.parse_warnings == []


def test_boilerplate_about_jll_is_removed() -> None:
    a = load_article("realterm-kauft-ehemaliges-zerres-logistikzentrum-in-moenchengladbach")
    assert all("NYSE" not in p for p in a.paragraphs)
    assert all(not p.startswith("Über JLL") for p in a.paragraphs)


def test_malformed_json_ld_falls_back_to_dateline_date_and_is_flagged() -> None:
    a = load_article("synthetic-malformed-jsonld-dateline-only")
    assert a.published_at_source == "dateline"
    assert a.published_at is not None and a.published_at.date() in {date(2025, 3, 2), date(2025, 3, 3)}
    assert "json_ld_malformed" in a.parse_warnings
    assert "json_ld_article_missing" in a.parse_warnings


def test_missing_body_raises() -> None:
    with pytest.raises(ParseError) as exc:
        parse_article((HTML / "synthetic-no-body.html").read_text(), "https://x/de-de/newsroom/no-body")
    assert exc.value.kind == "missing_body"


@pytest.mark.parametrize("html", ["", "   "])
def test_empty_document_raises(html: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse_article(html, "https://x")
    assert exc.value.kind == "empty_document"


def test_missing_title_raises() -> None:
    with pytest.raises(ParseError) as exc:
        parse_article(
            "<html><body><div class='editorial__body'><p>Text ohne Titel</p></div></body></html>", "https://x"
        )
    assert exc.value.kind == "missing_title"


def test_date_mismatch_between_json_ld_and_dateline_is_flagged() -> None:
    html = """<html><head><title>T</title><script type="application/ld+json">
      [{"@type":"NewsArticle","headline":"Käufer erwirbt Areal","datePublished":"2025-01-20T06:00:00Z"}]</script></head>
      <body><div class="editorial__body"><p>BERLIN, 2. Januar 2025 – Ein Käufer hat ein Areal erworben.</p></div></body></html>"""
    a = parse_article(html, "https://www.jll.com/de-de/newsroom/x")
    assert any(w.startswith("date_mismatch") for w in a.parse_warnings)
    # the press release's own dateline wins; the CMS date is kept as evidence
    assert a.published_at_source == "dateline"
    assert a.published_at is not None and a.published_at.date() in {date(2025, 1, 1), date(2025, 1, 2)}
    assert "jsonld_date_published:2025-01-20T06:00:00+00:00" in a.parse_warnings


def test_future_publication_date_is_flagged() -> None:
    html = """<html><head><title>T</title><script type="application/ld+json">
      [{"@type":"NewsArticle","headline":"X","datePublished":"2999-01-01T00:00:00Z"}]</script></head>
      <body><div class="editorial__body"><p>Text</p></div></body></html>"""
    assert "published_at_in_future" in parse_article(html, "https://x").parse_warnings


@pytest.mark.parametrize(
    ("paragraph", "place", "day"),
    [
        ("MÖNCHENGLADBACH, 16. Juli 2026 – Der Investor …", "MÖNCHENGLADBACH", date(2026, 7, 16)),
        ("FRANKFURT/MAIN, 3. März 2025 - Text", "FRANKFURT/MAIN", date(2025, 3, 3)),
        ("Frankfurt , 16. November 2023 – Principal …", "Frankfurt", date(2023, 11, 16)),
        ("BERLIN, 05.02.2024 – Text", "BERLIN", date(2024, 2, 5)),
        ("BERLIN, 31. Februar 2024 – invalid day", "BERLIN", None),
        ("Kein Dateline-Absatz.", None, None),
    ],
)
def test_parse_dateline(paragraph: str, place: str | None, day: date | None) -> None:
    assert parse_dateline(paragraph) == (place, day)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.jll.com/de-de/newsroom/Saller-Unternehmensgruppe-kauft",
            "newsroom/saller-unternehmensgruppe-kauft",
        ),
        ("https://www.jll.com/de-de/newsroom/abc/", "newsroom/abc"),
        ("https://www.jll.com/en-belux/newsroom/x", "newsroom/x"),
    ],
)
def test_source_record_id_is_stable_and_case_insensitive(url: str, expected: str) -> None:
    assert source_record_id(url) == expected
