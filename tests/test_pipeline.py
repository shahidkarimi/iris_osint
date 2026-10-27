from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jll_harvester.classify import SignalClassifier
from jll_harvester.config import Settings
from jll_harvester.geocode import LocationResolver
from jll_harvester.normalize import build_signal, content_hash, quality_flags
from jll_harvester.pipeline import FixtureSource, Harvester

from .conftest import HTML, FakeRepository, load_article


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(HTML, target)
    return target


def make_harvester(settings: Settings, repo: FakeRepository, resolver: LocationResolver, directory: Path) -> Harvester:
    return Harvester(
        settings=settings,
        repository=repo,  # type: ignore[arg-type]
        source=FixtureSource(directory, settings.section_prefixes),
        classifier=SignalClassifier(),
        resolver=resolver,
    )


def test_fixture_run_produces_signals_rejections_and_visible_errors(
    settings, fake_repo, offline_resolver, fixture_dir
) -> None:
    report = make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run()
    c = report.counters
    # 18 fixture files + a sitemap entry whose page is missing; ?page=2, /services and the index page are filtered out
    assert c["discovered"] == 19
    assert c["signal_inserted"] == 10
    assert c["fetch_error"] == 2  # missing-page-404 + synthetic-empty
    assert c["parse_error"] == 1  # synthetic-no-body
    assert report.status == "partial"  # errors are visible, the run still completes
    assert report.fetch_errors == {"not_found": 1, "empty_response": 1}
    assert report.rejections["market_research"] == 1
    assert {i.code for i in report.issues} >= {
        "not_found",
        "empty_response",
        "missing_body",
        "location_ambiguous",
        "location_unresolved",
    }
    assert not fake_repo.locked


def test_rerun_is_idempotent_and_incremental(settings, fake_repo, offline_resolver, fixture_dir) -> None:
    make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run()
    signals_after_first = dict(fake_repo.signals)

    second = make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run()
    assert second.counters["signal_inserted"] == 0
    assert second.counters["skipped_unchanged"] == 16  # 10 signals + 6 rejections: unchanged <lastmod>
    assert second.counters["fetch_error"] == 2 and second.counters["parse_error"] == 1  # failures are retried
    assert fake_repo.signals.keys() == signals_after_first.keys()

    full = make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run(full=True)
    assert full.counters["signal_inserted"] == 0
    assert full.counters["signal_unchanged"] == 10  # re-processed, but nothing duplicated or changed
    assert len(fake_repo.signals) == 10


def test_changed_article_creates_new_record_version(settings, fake_repo, offline_resolver, fixture_dir) -> None:
    make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run()
    page = fixture_dir / "henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg.html"
    page.write_text(
        page.read_text().replace("Henkel verkauft zwei Gewerbegrundstücke", "Henkel veräußert zwei Gewerbegrundstücke")
    )

    report = make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run(full=True)
    assert report.counters["signal_updated"] == 1
    record = fake_repo.signals[("jll_de_newsroom", "newsroom/henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg")]
    assert record["version"] == 2


def test_signal_is_retracted_when_no_longer_relevant(settings, fake_repo, offline_resolver, fixture_dir) -> None:
    make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run()
    page = fixture_dir / "synthetic-region-only.html"
    page.write_text(
        page.read_text()
        .replace("Gewerbegrundstück", "Bürogebäude")
        .replace("jll-taxonomy:property-types/industrial-and-logistics", "jll-taxonomy:property-types/office")
    )

    report = make_harvester(settings, fake_repo, offline_resolver, fixture_dir).run(full=True)
    assert report.counters["retracted"] == 1
    assert fake_repo.signals[("jll_de_newsroom", "newsroom/synthetic-region-only")]["retracted"]


def test_discovery_failure_fails_the_run_cleanly(settings, fake_repo, offline_resolver, tmp_path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    (empty / "sitemap.xml").write_text("<urlset><broken")
    report = make_harvester(settings, fake_repo, offline_resolver, empty).run()
    assert report.status == "failed"
    assert any(i.stage == "discovery" for i in report.issues)
    assert not fake_repo.locked


# ---------------------------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------------------------
def _signal(classifier, offline_resolver, slug: str, raw_sha: str = "a" * 64):  # type: ignore[no-untyped-def]
    article = load_article(slug)
    return build_signal(
        article=article,
        classification=classifier.classify(article),
        location=offline_resolver.resolve(article),
        task_id="IRIS-SIG-45",
        source_system="jll_de_newsroom",
        source_lastmod=None,
        raw_sha256=raw_sha,
    )


def test_content_hash_ignores_fetch_artifacts(classifier, offline_resolver) -> None:
    slug = "universa-versicherung-kauft-grundstueck-in-nuernberg-fuer-neue-firmenzentrale"
    a = _signal(classifier, offline_resolver, slug, "a" * 64)
    b = _signal(classifier, offline_resolver, slug, "b" * 64)
    assert a.content_hash == b.content_hash
    assert content_hash({"title": "x"}) != content_hash({"title": "y"})


def test_quality_flags_surface_fallbacks(classifier, offline_resolver) -> None:
    article = load_article("synthetic-malformed-jsonld-dateline-only")
    flags = quality_flags(article, classifier.classify(article), offline_resolver.resolve(article))
    assert "published_at_from_dateline" in flags
    assert "json_ld_malformed" in flags

    article = load_article("synthetic-region-only")
    flags = quality_flags(article, classifier.classify(article), offline_resolver.resolve(article))
    assert "location_unresolved" in flags


def test_rejected_classification_cannot_become_a_signal(classifier, offline_resolver) -> None:
    article = load_article("logistikinvestmentmarkt-bestaetigt-aufwaertstrend")
    with pytest.raises(ValueError, match="rejected"):
        build_signal(
            article=article,
            classification=classifier.classify(article),
            location=offline_resolver.resolve(article),
            task_id="t",
            source_system="s",
            source_lastmod=None,
            raw_sha256=None,
        )


def test_reprocess_from_snapshots_needs_no_source_access(settings, fake_repo, offline_resolver, tmp_path) -> None:
    import gzip
    import hashlib

    from jll_harvester.models import SitemapEntry
    from jll_harvester.pipeline import SnapshotSource

    slug = "henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg"
    html = (HTML / f"{slug}.html").read_bytes()
    sha = hashlib.sha256(html).hexdigest()
    (tmp_path / f"{sha}.html.gz").write_bytes(gzip.compress(html))
    entries = [
        (SitemapEntry(url=f"https://www.jll.com/de-de/newsroom/{slug}"), sha),
        (SitemapEntry(url="https://www.jll.com/de-de/newsroom/gone"), "0" * 64),
    ]
    harvester = Harvester(
        settings=settings,
        repository=fake_repo,  # type: ignore[arg-type]
        source=SnapshotSource(tmp_path, entries),
        classifier=SignalClassifier(),
        resolver=offline_resolver,
    )
    report = harvester.run(full=True)
    assert report.mode == "reprocess"
    assert report.counters["signal_inserted"] == 1
    assert report.fetch_errors == {"snapshot_missing": 1}
