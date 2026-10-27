"""Trim a downloaded JLL page to the parts the parser uses, for use as a test fixture.

Keeps <title>, the relevant <meta>/<link rel=canonical>, JSON-LD and the `.editorial__body`
blocks (including the "Über JLL" boilerplate, so boilerplate stripping stays under test).
Navigation, scripts, tracking and styling are dropped — fixtures stay small and reviewable.

    python scripts/make_fixture.py page.html tests/fixtures/html/<slug>.html
"""

from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup

KEEP_META_NAMES = {"description", "keywords", "ch.tagcloud", "template"}


def trim(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    out = BeautifulSoup("<!doctype html><html><head></head><body><main></main></body></html>", "lxml")
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        out.html["lang"] = html_tag["lang"]
    head, main = out.head, out.main
    if soup.title:
        head.append(soup.title)
    for meta in soup.find_all("meta"):
        if meta.get("name") in KEEP_META_NAMES or str(meta.get("property", "")).startswith("og:"):
            head.append(meta)
    canonical = soup.find("link", rel="canonical")
    if canonical:
        head.append(canonical)
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        head.append(script)
    h1 = soup.find("h1")
    if h1:
        main.append(h1)
    for block in soup.select(".editorial__body"):
        wrapper = out.new_tag("div", attrs={"class": "editorial__body"})
        for el in block.find_all(["h2", "h3", "p", "li"]):
            if el.find_parent("li") is not None and el.name == "p":
                continue
            new = out.new_tag(el.name)
            new.string = el.get_text(" ", strip=True)
            wrapper.append(new)
        main.append(wrapper)
    return out.prettify()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    Path(sys.argv[2]).write_text(trim(Path(sys.argv[1]).read_text(encoding="utf-8")), encoding="utf-8")
    print(f"wrote {sys.argv[2]}")
