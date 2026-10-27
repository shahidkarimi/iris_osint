"""Command-line entry point: ``jll-harvester --help``."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Annotated

import typer

from . import logging_setup
from .classify import SignalClassifier
from .config import GeocoderMode, Settings, get_settings
from .db import Repository, connect, migrate, queries
from .db.repository import GeocodeCache, HarvestAlreadyRunning
from .gazetteer import Gazetteer
from .geocode import GazetteerGeocoder, Geocoder, LocationResolver, NominatimGeocoder
from .http import PoliteClient
from .models import SitemapEntry
from .pipeline import DocumentSource, FixtureSource, Harvester, LiveSource, RunReport, SnapshotSource
from .report import build_report, render_markdown, signals_csv, signals_jsonl

app = typer.Typer(add_completion=False, no_args_is_help=True, help="IRIS-SIG-45 · JLL Germany newsroom harvester")
log = logging.getLogger("jll_harvester")


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v", help="debug logging")] = False) -> None:
    logging_setup.configure("DEBUG" if verbose else None)


def _resolver(settings: Settings, cache: GeocodeCache | None, mode: GeocoderMode) -> LocationResolver:
    gazetteer = Gazetteer.bundled()
    geocoders: list[Geocoder] = []
    if mode == GeocoderMode.NOMINATIM:
        geocoders.append(
            NominatimGeocoder(
                base_url=settings.nominatim_url,
                user_agent=settings.user_agent,
                email=settings.nominatim_email,
                delay_seconds=settings.nominatim_delay_seconds,
                cache=cache,
            )
        )
    if mode != GeocoderMode.NONE:
        geocoders.append(GazetteerGeocoder(gazetteer))
    return LocationResolver(gazetteer, geocoders)


@app.command("migrate")
def migrate_cmd() -> None:
    """Create / upgrade the iris_staging schema (idempotent)."""
    settings = get_settings()
    with connect(settings.dsn) as conn:
        applied = migrate(conn)
    typer.echo(f"migrations applied: {', '.join(applied) or 'none (schema up to date)'}")


@app.command("check-source")
def check_source_cmd() -> None:
    """Verify the brief's endpoint, robots.txt and sitemap without harvesting."""
    settings = get_settings()
    with PoliteClient(user_agent=settings.user_agent, delay_seconds=settings.request_delay_seconds) as client:
        source = LiveSource(settings, client)
        result = source.check()
        entries = source.entries()
    result["sitemap_entries_in_scope"] = len(entries)
    result["newest"] = [{"url": e.url, "lastmod": e.lastmod.isoformat() if e.lastmod else None} for e in entries[:5]]
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("harvest")
def harvest_cmd(
    full: Annotated[bool, typer.Option(help="re-process every URL, ignoring sitemap <lastmod>")] = False,
    limit: Annotated[int, typer.Option(help="process at most N documents (0 = all)")] = 0,
    fixtures: Annotated[
        Path | None, typer.Option(help="offline mode: directory with sitemap.xml + <slug>.html")
    ] = None,
    geocoder: Annotated[GeocoderMode | None, typer.Option(help="override IRIS_GEOCODER")] = None,
) -> None:
    """Run one harvest (incremental by default)."""
    settings = get_settings()
    report = _harvest(
        settings, full=full, limit=limit or settings.max_documents_per_run or None, fixtures=fixtures, geocoder=geocoder
    )
    typer.echo(
        json.dumps({"run_id": report.run_id, "status": report.status, **report.stats()}, indent=2, ensure_ascii=False)
    )
    if report.status == "failed":
        raise typer.Exit(code=1)


def _harvest(
    settings: Settings, *, full: bool, limit: int | None, fixtures: Path | None, geocoder: GeocoderMode | None
) -> RunReport:
    with connect(settings.dsn, autocommit=True) as conn:
        migrate(conn)
        repo = Repository(conn)
        mode = geocoder or (GeocoderMode.OFFLINE if fixtures else settings.geocoder)
        resolver = _resolver(settings, GeocodeCache(conn), mode)
        client: PoliteClient | None = None
        source: DocumentSource
        if fixtures:
            source = FixtureSource(fixtures, settings.section_prefixes)
        else:
            client = PoliteClient(
                user_agent=settings.user_agent,
                delay_seconds=settings.request_delay_seconds,
                timeout_seconds=settings.request_timeout_seconds,
                max_retries=settings.max_retries,
                backoff_base_seconds=settings.backoff_base_seconds,
            )
            source = LiveSource(settings, client)
        harvester = Harvester(
            settings=settings,
            repository=repo,
            source=source,
            classifier=SignalClassifier(
                big_box_min_sqm=settings.big_box_min_sqm, min_confidence=settings.min_classification_confidence
            ),
            resolver=resolver,
        )
        try:
            return harvester.run(full=full, limit=limit)
        except HarvestAlreadyRunning as exc:
            typer.echo(f"skipped: {exc}", err=True)
            raise typer.Exit(code=75) from exc
        finally:
            if client:
                client.close()


@app.command("reprocess")
def reprocess_cmd(
    snapshots: Annotated[Path | None, typer.Option(help="snapshot directory (default: IRIS_RAW_SNAPSHOT_DIR)")] = None,
    geocoder: Annotated[GeocoderMode | None, typer.Option(help="override IRIS_GEOCODER")] = None,
) -> None:
    """Re-run parse → classify → locate → load from stored snapshots (no requests to the source)."""
    settings = get_settings()
    directory = snapshots or settings.raw_snapshot_dir
    if directory is None:
        raise typer.BadParameter("no snapshot directory configured")
    with connect(settings.dsn, autocommit=True) as conn:
        migrate(conn)
        repo = Repository(conn)
        rows = repo.snapshot_documents(settings.source_system)
        source = SnapshotSource(
            directory, [(SitemapEntry(url=r["url"], lastmod=r["sitemap_lastmod"]), r["raw_sha256"]) for r in rows]
        )
        harvester = Harvester(
            settings=settings,
            repository=repo,
            source=source,
            classifier=SignalClassifier(
                big_box_min_sqm=settings.big_box_min_sqm, min_confidence=settings.min_classification_confidence
            ),
            resolver=_resolver(settings, GeocodeCache(conn), geocoder or settings.geocoder),
        )
        report = harvester.run(full=True)
    typer.echo(
        json.dumps({"run_id": report.run_id, "status": report.status, **report.stats()}, indent=2, ensure_ascii=False)
    )


@app.command("schedule")
def schedule_cmd(
    every: Annotated[int, typer.Option(help="seconds between incremental harvests")] = 6 * 3600,
) -> None:
    """Run incremental harvests forever (used by the docker `scheduler` service)."""
    settings = get_settings()
    while True:
        try:
            report = _harvest(
                settings, full=False, limit=settings.max_documents_per_run or None, fixtures=None, geocoder=None
            )
            log.info("scheduled harvest finished", extra={"run_id": report.run_id, "status": report.status})
        except typer.Exit:
            pass
        except Exception:
            log.exception("scheduled harvest crashed; will retry next cycle")
        time.sleep(every)


@app.command("validate")
def validate_cmd() -> None:
    """Run the validation queries (db/validation.sql); exit 1 if an error-level check fails."""
    settings = get_settings()
    with connect(settings.dsn, autocommit=True) as conn:
        results = queries.run_checks(conn)
    failed = False
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        typer.echo(
            f"[{mark}] {r.id:<34} {r.severity:<8} value={r.value}  {r.description}"
            + (f"  ({r.error})" if r.error else "")
        )
        failed |= (not r.passed) and r.severity == "error"
    if failed:
        raise typer.Exit(code=1)


@app.command("report")
def report_cmd(
    fmt: Annotated[str, typer.Option("--format", help="md | json")] = "md",
    out: Annotated[Path | None, typer.Option(help="write to file instead of stdout")] = None,
    run_id: Annotated[str | None, typer.Option(help="report on a specific run")] = None,
) -> None:
    """Error / data-quality report for the latest (or given) run."""
    settings = get_settings()
    with connect(settings.dsn, autocommit=True) as conn:
        data = build_report(conn, run_id)
    text = render_markdown(data) if fmt == "md" else json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@app.command("export")
def export_cmd(
    out_dir: Annotated[Path, typer.Option(help="directory for signals.geojson / .csv / .jsonl")] = Path("samples"),
) -> None:
    """Export staged signals as GeoJSON (WGS84), CSV and JSONL (both incl. EPSG:25832 x/y)."""
    settings = get_settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    with connect(settings.dsn, autocommit=True) as conn:
        rows, _ = queries.list_signals(conn, queries.SignalFilter(), limit=100_000)
        fc = queries.geojson(conn, queries.SignalFilter())
    (out_dir / "signals.geojson").write_text(
        json.dumps(fc, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    (out_dir / "signals.csv").write_text(signals_csv(rows), encoding="utf-8")
    (out_dir / "signals.jsonl").write_text(signals_jsonl(rows), encoding="utf-8")
    typer.echo(f"exported {len(rows)} signals ({len(fc['features'])} with geometry) to {out_dir}/")


@app.command("serve")
def serve_cmd(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
    reload: Annotated[bool, typer.Option()] = False,
) -> None:
    """Start the web dashboard."""
    import uvicorn

    uvicorn.run(
        "jll_harvester.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    app()
