from jll_harvester.robots import RobotsPolicy

JLL_ROBOTS = """
User-agent: *
Disallow: */content-search
Disallow: /content/dam/jllcom/
Allow: /content/dam/jllcom/en/us/documents/reports/research-reports/
Disallow: /*?

Sitemap: https://www.jll.com/sitemap-index.xml
"""


def test_query_strings_are_disallowed_like_jll_robots() -> None:
    policy = RobotsPolicy.parse(JLL_ROBOTS, "IRIS-SIG-45-Harvester/1.0")
    assert policy.can_fetch("https://www.jll.com/de-de/newsroom/some-article")
    assert policy.can_fetch("https://www.jll.com/de-de/sitemap.xml")
    assert not policy.can_fetch("https://www.jll.com/de-de/newsroom?page=2")
    assert not policy.can_fetch("https://www.jll.com/de-de/insights/market-dynamics?tag=x")


def test_wildcard_prefix_and_longest_match_allow_wins() -> None:
    policy = RobotsPolicy.parse(JLL_ROBOTS, "bot")
    assert not policy.can_fetch("https://www.jll.com/de-de/content-search")
    assert not policy.can_fetch("https://www.jll.com/content/dam/jllcom/de/file.pdf")
    assert policy.can_fetch("https://www.jll.com/content/dam/jllcom/en/us/documents/reports/research-reports/r.pdf")


def test_sitemaps_and_crawl_delay_are_parsed() -> None:
    policy = RobotsPolicy.parse("User-agent: *\nCrawl-delay: 4\nDisallow: /private\nSitemap: https://x/sm.xml", "bot")
    assert policy.sitemaps == ["https://x/sm.xml"]
    assert policy.crawl_delay == 4.0


def test_specific_user_agent_group_takes_precedence() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: iris-sig-45-harvester\nAllow: /\n"
    assert RobotsPolicy.parse(text, "IRIS-SIG-45-Harvester/1.0").can_fetch("https://x/a")
    assert not RobotsPolicy.parse(text, "OtherBot/1.0").can_fetch("https://x/a")


def test_end_anchor_and_empty_disallow() -> None:
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /*.pdf$\nDisallow:\n", "bot")
    assert not policy.can_fetch("https://x/report.pdf")
    assert policy.can_fetch("https://x/report.pdf.html")
    assert policy.can_fetch("https://x/anything")
