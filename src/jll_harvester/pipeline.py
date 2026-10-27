"""Harvest orchestration: discover → (incremental skip) → fetch → parse → classify → locate → load.

Each document is processed in its own transaction, so one bad page (network failure, malformed
HTML, constraint violation) is recorded as an issue and never aborts the run or poisons others.
"""

from __future__ import annotations

import gzip
import logging
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from .classify import SignalClassifier
from .config import Settings
from .db.repository import Repository, UpsertOutcome
from .discovery import DiscoveryError, discover, filter_section, parse_sitemap
from .geocode import LocationResolver
from .http import FetchError, PoliteClient
from .models import Issue, IssueSeverity, RawDocument, SitemapEntry
from .normalize import build_signal, is_german
from .parsing import ParseError, parse_article, source_record_id

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------------------------
# Document sources
# ---------------------------------------------------------------------------------------------
class DocumentSource(Protocol):
    mode: str

    def check(self) -> dict[str, Any]: ...

    def entries(self) -> list[SitemapEntry]: ...

    def fetch(self, entry: SitemapEntry) -> RawDocument: ...


class LiveSource:
    """The real JLL site, via the robots-advertised sitemap."""

    mode = "live"

    def __init__(self, settings: Settings, client: PoliteClient) -> None:
        self.settings = settings
        self.client = client

    def check(self) -> dict[str, Any]:
        """Verify — not assume — the state of the brief's endpoint and the access policy."""
        result: dict[str, Any] = {
            "checked_at": datetime.now(UTC).isoformat(),
            "brief_endpoint": self.settings.brief_endpoint,
        }
        try:
            with httpx.Client(headers={"User-Agent": self.settings.user_agent}, timeout=20, follow_redirects=True) as c:
                resp = c.get(self.settings.brief_endpoint)
            result["redirect_chain"] = [{"status": r.status_code, "url": str(r.url)} for r in resp.history]
            result["final_url"] = str(resp.url)
            result["final_status"] = resp.status_code
            result["listing_is_client_rendered"] = "editorial__body" not in resp.text and "/newsroom/" not in resp.text
        except httpx.HTTPError as exc:
            result["error"] = f"{exc.__class__.__name__}: {exc}"
        try:
            robots = self.client.robots_for(self.settings.sitemap_url)
            result["robots"] = {
                "rules": len(robots.rules),
                "disallow_query_strings": not robots.can_fetch(f"{self.settings.base_url}/de-de/newsroom?page=2"),
                "sitemap_allowed": robots.can_fetch(self.settings.sitemap_url),
                "crawl_delay": robots.crawl_delay,
                "sitemaps_advertised": robots.sitemaps[:5],
            }
        except FetchError as exc:
            result["robots_error"] = str(exc)
        return result

    def entries(self) -> list[SitemapEntry]:
        return discover(self.client, self.settings.sitemap_url, self.settings.section_prefixes)

    def fetch(self, entry: SitemapEntry) -> RawDocument:
        return self.client.fetch(entry.url)


class FixtureSource:
    """Offline source: ``<dir>/sitemap.xml`` + ``<dir>/<slug>.html``. Used for demos and tests."""

    mode = "fixture"

    def __init__(self, directory: Path, prefixes: list[str]) -> None:
        self.directory = directory
        self.prefixes = prefixes

    def check(self) -> dict[str, Any]:
        return {"fixture_directory": str(self.directory), "checked_at": datetime.now(UTC).isoformat()}

    def entries(self) -> list[SitemapEntry]:
        sitemap = self.directory / "sitemap.xml"
        entries, _ = parse_sitemap(sitemap.read_bytes(), str(sitemap))
        return filter_section(entries, self.prefixes)

    def fetch(self, entry: SitemapEntry) -> RawDocument:
        slug = entry.url.rstrip("/").rsplit("/", 1)[-1]
        path = self.directory / f"{slug}.html"
        if not path.exists():
            raise FetchError(entry.url, "not_found", f"fixture {path.name} missing", 404)
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise FetchError(entry.url, "empty_response", "fixture is empty", 200)
        import hashlib

        return RawDocument(
            url=entry.url,
            final_url=entry.url,
            status_code=200,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            fetched_at=datetime.now(UTC),
            content_type="text/html",
        )


class SnapshotSource:
    """Re-process previously fetched pages from their gzip snapshots — no request reaches the source.

    Used after rule changes (classification, gazetteer, geocoding) so improvements reach staging
    without re-crawling. Entries come from the crawl ledger (``source_document.raw_sha256``).
    """

    mode = "reprocess"

    def __init__(self, directory: Path, entries: list[tuple[SitemapEntry, str]]) -> None:
        self.directory = directory
        self._entries = entries
        self._sha = {entry.url: sha for entry, sha in entries}

    def check(self) -> dict[str, Any]:
        return {
            "snapshot_directory": str(self.directory),
            "documents": len(self._entries),
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def entries(self) -> list[SitemapEntry]:
        return [entry for entry, _ in self._entries]

    def fetch(self, entry: SitemapEntry) -> RawDocument:
        sha = self._sha[entry.url]
        path = self.directory / f"{sha}.html.gz"
        if not path.exists():
            raise FetchError(entry.url, "snapshot_missing", f"no snapshot {path.name}")
        content = gzip.decompress(path.read_bytes()).decode("utf-8")
        return RawDocument(
            url=entry.url,
            final_url=entry.url,
            status_code=200,
            content=content,
            sha256=sha,
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
            content_type="text/html",
        )


# ---------------------------------------------------------------------------------------------
# Run report
# ---------------------------------------------------------------------------------------------
@dataclass
class RunReport:
    run_id: str
    mode: str
    status: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    counters: Counter[str] = field(default_factory=Counter)
    rejections: Counter[str] = field(default_factory=Counter)
    fetch_errors: Counter[str] = field(default_factory=Counter)
    geocode: Counter[str] = field(default_factory=Counter)
    signal_types: Counter[str] = field(default_factory=Counter)
    issues: list[Issue] = field(default_factory=list)
    source_check: dict[str, Any] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        duration = ((self.finished_at or datetime.now(UTC)) - self.started_at).total_seconds()
        return {
            "duration_seconds": round(duration, 1),
            "counters": dict(self.counters),
            "rejections": dict(self.rejections),
            "fetch_errors": dict(self.fetch_errors),
            "geocode_status": dict(self.geocode),
            "signal_types": dict(self.signal_types),
            "issues_by_severity": dict(Counter(i.severity.value for i in self.issues)),
        }


# ---------------------------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------------------------
class Harvester:
    def __init__(
        self,
        *,
        settings: Settings,
        repository: Repository,
        source: DocumentSource,
        classifier: SignalClassifier,
        resolver: LocationResolver,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.source = source
        self.classifier = classifier
        self.resolver = resolver

    def run(self, *, full: bool = False, limit: int | None = None) -> RunReport:
        repo = self.repo
        repo.acquire_harvest_lock()
        config = {
            "full": full,
            "limit": limit,
            "geocoder": self.settings.geocoder.value,
            "big_box_min_sqm": self.settings.big_box_min_sqm,
            "min_classification_confidence": self.settings.min_classification_confidence,
            "section_prefixes": self.settings.section_prefixes,
        }
        run_id = repo.start_run(
            task_id=self.settings.task_id,
            source_system=self.settings.source_system,
            mode=self.source.mode,
            config=config,
        )
        report = RunReport(run_id=str(run_id), mode=self.source.mode)
        error: str | None = None
        try:
            report.source_check = self.source.check()
            repo.record_source_check(run_id, report.source_check)
            try:
                entries = self.source.entries()
            except (DiscoveryError, FetchError, OSError) as exc:
                report.issues.append(
                    Issue(
                        severity=IssueSeverity.ERROR,
                        stage="discovery",
                        code=getattr(exc, "kind", "discovery_failed"),
                        message=str(exc),
                    )
                )
                raise
            report.counters["discovered"] = len(entries)
            if not entries:
                report.issues.append(
                    Issue(
                        severity=IssueSeverity.WARNING,
                        stage="discovery",
                        code="empty_discovery",
                        message="sitemap yielded no article URLs for the configured sections",
                    )
                )

            known = repo.known_documents(self.settings.source_system)
            todo = list(self._select(entries, known, full, report))
            if limit:
                todo = todo[:limit]
            for i, entry in enumerate(todo, 1):
                self._process(entry, run_id, report)
                if i % 25 == 0:
                    log.info("progress", extra={"done": i, "total": len(todo), **dict(report.signal_types)})
            if self.resolver.provider_failures:
                report.issues.append(
                    Issue(
                        severity=IssueSeverity.WARNING,
                        stage="geocode",
                        code="geocoder_unavailable",
                        message=f"{len(self.resolver.provider_failures)} geocoder calls failed; offline gazetteer fallback used",
                        details={"examples": self.resolver.provider_failures[:5]},
                    )
                )
        except (DiscoveryError, FetchError) as exc:  # expected source-side failures: no traceback noise
            error = f"{exc.__class__.__name__}: {exc}"
            log.error("harvest failed at discovery", extra={"kind": exc.kind, "error": str(exc)})
            repo.conn.rollback()
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            log.exception("harvest failed")
            repo.conn.rollback()
        finally:
            report.finished_at = datetime.now(UTC)
            hard_errors = report.counters["fetch_error"] + report.counters["parse_error"] + report.counters["db_error"]
            report.status = "failed" if error else ("partial" if hard_errors else "succeeded")
            repo.add_issues(run_id, report.issues)
            repo.finish_run(
                run_id, status=report.status, stats=report.stats(), source_check=report.source_check, error=error
            )
            repo.release_harvest_lock()
            repo.conn.commit()
        return report

    # -- incremental selection --------------------------------------------------------------
    def _select(
        self, entries: list[SitemapEntry], known: dict[str, dict[str, Any]], full: bool, report: RunReport
    ) -> Iterator[SitemapEntry]:
        for entry in entries:
            prior = known.get(source_record_id(entry.url))
            unchanged = (
                prior is not None
                and prior["decision"] in {"signal", "rejected"}
                and entry.lastmod is not None
                and prior["sitemap_lastmod"] == entry.lastmod
            )
            if unchanged and not full:
                report.counters["skipped_unchanged"] += 1
                continue
            yield entry

    # -- per document -----------------------------------------------------------------------
    def _process(self, entry: SitemapEntry, run_id: Any, report: RunReport) -> None:
        repo, settings = self.repo, self.settings
        rid = source_record_id(entry.url)
        doc_kwargs: dict[str, Any] = {
            "run_id": run_id,
            "source_system": settings.source_system,
            "source_record_id": rid,
            "url": entry.url,
            "sitemap_lastmod": entry.lastmod,
        }

        # fetch
        try:
            raw = self.source.fetch(entry)
            report.counters["fetched"] += 1
        except FetchError as exc:
            report.counters["fetch_error"] += 1
            report.fetch_errors[exc.kind] += 1
            severity = IssueSeverity.WARNING if exc.kind in {"not_found", "robots_disallowed"} else IssueSeverity.ERROR
            report.issues.append(
                Issue(severity=severity, stage="fetch", code=exc.kind, message=str(exc), source_url=entry.url)
            )
            with repo.conn.transaction():
                repo.upsert_document(
                    **doc_kwargs, decision="fetch_error", decision_reason=exc.kind, http_status=exc.status_code
                )
            return
        self._snapshot(raw)

        # parse
        try:
            article = parse_article(raw.content, raw.final_url or entry.url)
        except ParseError as exc:
            report.counters["parse_error"] += 1
            report.issues.append(
                Issue(
                    severity=IssueSeverity.ERROR, stage="parse", code=exc.kind, message=str(exc), source_url=entry.url
                )
            )
            with repo.conn.transaction():
                repo.upsert_document(
                    **doc_kwargs,
                    decision="parse_error",
                    decision_reason=exc.kind,
                    fetched_at=raw.fetched_at,
                    http_status=raw.status_code,
                    raw_sha256=raw.sha256,
                )
            return
        doc_kwargs.update(
            fetched_at=raw.fetched_at,
            http_status=raw.status_code,
            raw_sha256=raw.sha256,
            title=article.title,
            published_at=article.published_at,
        )
        for warning in article.parse_warnings:
            if warning.startswith(
                ("date_mismatch", "published_at_missing", "json_ld_malformed", "published_at_in_future")
            ):
                report.issues.append(
                    Issue(
                        severity=IssueSeverity.WARNING,
                        stage="parse",
                        code=warning.split("_jsonld")[0],
                        message=f"{warning}: {article.title}",
                        source_url=entry.url,
                    )
                )

        # classify
        cls = self.classifier.classify(article)
        location = None
        if cls.is_signal:
            location = self.resolver.resolve(article)
            if not is_german(article, location):
                cls = cls.model_copy(update={"signal_type": None, "rejection_reason": "outside_germany"})

        try:
            with repo.conn.transaction():
                if not cls.is_signal or location is None:
                    reason = cls.rejection_reason or "rejected"
                    report.counters["rejected"] += 1
                    report.rejections[reason] += 1
                    repo.upsert_document(
                        **doc_kwargs,
                        decision="rejected",
                        decision_reason=reason,
                        classification=cls.model_dump(mode="json"),
                    )
                    if repo.retract_signal(settings.source_system, rid, f"reclassified: {reason}"):
                        report.counters["retracted"] += 1
                        report.issues.append(
                            Issue(
                                severity=IssueSeverity.INFO,
                                stage="load",
                                code="signal_retracted",
                                message=f"no longer a signal ({reason}): {article.title}",
                                source_url=entry.url,
                            )
                        )
                    return

                signal = build_signal(
                    article=article,
                    classification=cls,
                    location=location,
                    task_id=settings.task_id,
                    source_system=settings.source_system,
                    source_lastmod=entry.lastmod,
                    raw_sha256=raw.sha256,
                )
                outcome = repo.upsert_signal(signal, run_id)
                repo.upsert_document(
                    **doc_kwargs,
                    decision="signal",
                    signal_type=signal.signal_type.value,
                    classification=cls.model_dump(mode="json"),
                )
        except Exception as exc:  # constraint violations etc. — isolate to this document
            report.counters["db_error"] += 1
            report.issues.append(
                Issue(
                    severity=IssueSeverity.ERROR,
                    stage="load",
                    code="db_error",
                    message=f"{exc.__class__.__name__}: {exc}",
                    source_url=entry.url,
                )
            )
            log.exception("load failed", extra={"url": entry.url})
            return

        report.counters[f"signal_{outcome.value}"] += 1
        report.signal_types[signal.signal_type.value] += 1
        report.geocode[location.status.value] += 1
        if location.status.value != "resolved":
            severity = IssueSeverity.ERROR if location.status.value == "invalid" else IssueSeverity.WARNING
            report.issues.append(
                Issue(
                    severity=severity,
                    stage="geocode",
                    code=f"location_{location.status.value}",
                    message=f"{signal.title} — {'; '.join(location.notes) or location.status.value}",
                    source_url=entry.url,
                    details={"location_text": location.location_text, "candidates": location.candidates[:5]},
                )
            )
        if outcome == UpsertOutcome.UPDATED:
            report.issues.append(
                Issue(
                    severity=IssueSeverity.INFO,
                    stage="load",
                    code="signal_updated",
                    message=f"content changed → new record version: {signal.title}",
                    source_url=entry.url,
                )
            )

    def _snapshot(self, raw: RawDocument) -> None:
        directory = self.settings.raw_snapshot_dir
        if directory is None or self.source.mode != "live":
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{raw.sha256}.html.gz"
            if not path.exists():
                path.write_bytes(gzip.compress(raw.content.encode("utf-8")))
        except OSError as exc:  # snapshots are best-effort lineage, never fatal
            log.warning("raw snapshot failed", extra={"error": str(exc)})


def sleep_until_next(interval_seconds: float) -> None:
    time.sleep(max(1.0, interval_seconds))
