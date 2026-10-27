"""PostgreSQL 16 / PostGIS 3.4 integration tests.

Run with a disposable database whose name contains "test", e.g. via docker compose:

    docker compose --profile test run --rm tests
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from jll_harvester.classify import SignalClassifier
from jll_harvester.config import GeocoderMode, Settings
from jll_harvester.db import Repository, connect, migrate, queries
from jll_harvester.db.repository import GeocodeCache, UpsertOutcome
from jll_harvester.gazetteer import Gazetteer
from jll_harvester.geocode import GazetteerGeocoder, LocationResolver
from jll_harvester.models import Article, Signal
from jll_harvester.normalize import build_signal
from jll_harvester.pipeline import FixtureSource, Harvester
from jll_harvester.report import build_report, render_markdown

from .conftest import HTML, load_article

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection[Any]]:
    with connect(database_url, autocommit=True) as c:
        dbname = c.execute("SELECT current_database() AS d").fetchone()["d"]
        if "test" not in dbname:
            pytest.fail(f"refusing to reset schema in database {dbname!r} (name must contain 'test')")
        c.execute("DROP SCHEMA IF EXISTS iris_staging CASCADE")
        migrate(c)
        yield c


def harvest(conn: psycopg.Connection[Any], directory: Path, *, full: bool = False):  # type: ignore[no-untyped-def]
    settings = Settings(database_url=None, geocoder=GeocoderMode.OFFLINE, raw_snapshot_dir=None, _env_file=None)  # type: ignore[call-arg]
    gaz = Gazetteer.bundled()
    return Harvester(
        settings=settings,
        repository=Repository(conn),
        source=FixtureSource(directory, settings.section_prefixes),
        classifier=SignalClassifier(),
        resolver=LocationResolver(gaz, [GazetteerGeocoder(gaz)]),
    ).run(full=full)


def count(conn: psycopg.Connection[Any], sql: str) -> int:
    return int(conn.execute(sql).fetchone()["n"])


def test_migrations_are_idempotent(conn: psycopg.Connection[Any]) -> None:
    assert migrate(conn) == []
    assert count(conn, "SELECT count(*) AS n FROM iris_staging.schema_migration") == 1
    srid = conn.execute("SELECT Find_SRID('iris_staging', 'signal_ingest', 'geom') AS s").fetchone()["s"]
    assert srid == 25832


def test_fixture_harvest_twice_never_duplicates(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    directory = tmp_path / "fx"
    shutil.copytree(HTML, directory)

    first = harvest(conn, directory)
    assert first.counters["signal_inserted"] == 10
    total = count(conn, "SELECT count(*) AS n FROM iris_staging.signal_ingest")
    assert total == 10

    second = harvest(conn, directory)
    full = harvest(conn, directory, full=True)
    assert second.counters["signal_inserted"] == 0 and full.counters["signal_inserted"] == 0
    assert full.counters["signal_unchanged"] == 10
    assert count(conn, "SELECT count(*) AS n FROM iris_staging.signal_ingest") == total
    assert count(conn, "SELECT count(*) AS n FROM iris_staging.signal_ingest WHERE record_version <> 1") == 0
    assert count(conn, "SELECT count(*) AS n FROM iris_staging.ingest_run") == 3
    # the crawl ledger covers every discovered document, including failures
    assert count(conn, "SELECT count(*) AS n FROM iris_staging.source_document") == 19
    assert (
        count(
            conn,
            "SELECT count(*) AS n FROM iris_staging.source_document WHERE decision IN ('fetch_error','parse_error')",
        )
        == 3
    )

    results = {r.id: r for r in queries.run_checks(conn)}
    failing = [r for r in results.values() if not r.passed]
    assert failing == [], failing
    assert results["duplicate_natural_keys"].value == 0
    assert results["projection_roundtrip"].value == 0


def test_points_are_valid_25832_inside_germany(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    shutil.copytree(HTML, tmp_path / "fx")
    harvest(conn, tmp_path / "fx")
    rows = conn.execute(
        "SELECT ST_SRID(geom) AS srid, ST_X(ST_Transform(geom, 4326)) AS lon, ST_Y(ST_Transform(geom, 4326)) AS lat "
        "FROM iris_staging.signal_ingest WHERE geom IS NOT NULL"
    ).fetchall()
    assert rows
    for r in rows:
        assert r["srid"] == 25832
        assert 5.5 < r["lon"] < 15.6 and 47.0 < r["lat"] < 55.2
    statuses = {
        r["geocode_status"] for r in conn.execute("SELECT geocode_status FROM iris_staging.signal_ingest").fetchall()
    }
    assert {"resolved", "ambiguous", "unresolved"} <= statuses


@pytest.mark.parametrize(
    ("x", "y", "status"),
    [
        (100.0, 100.0, "resolved"),  # outside Germany / not UTM 32N → CHECK violation
        (356_400.0, 5_645_000.0, "ambiguous"),  # a point on a non-resolved record → CHECK violation
    ],
)
def test_database_rejects_invalid_coordinates(conn: psycopg.Connection[Any], x: float, y: float, status: str) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """INSERT INTO iris_staging.signal_ingest (task_id, source_system, source_record_id, source_url, canonical_url,
                   signal_type, title, classification_confidence, geocode_status, geocode_precision, content_hash, payload, geom)
               VALUES ('t','s','r','https://x','https://x','commercial_land_disposal','T',0.9,%s,'municipality','h','{}',
                       ST_SetSRID(ST_MakePoint(%s, %s), 25832))""",
            (status, x, y),
        )


def test_upsert_outcomes_and_versioning(conn: psycopg.Connection[Any]) -> None:
    repo = Repository(conn)
    run_id = repo.start_run(task_id="t", source_system="jll_de_newsroom", mode="test", config={})
    gaz = Gazetteer.bundled()
    article = load_article("henkel-verkauft-zwei-gewerbegrundstuecke-in-heidelberg")
    location = LocationResolver(gaz, [GazetteerGeocoder(gaz)]).resolve(article)
    cls = SignalClassifier().classify(article)

    def make(a: Article) -> Signal:
        return build_signal(
            article=a,
            classification=cls,
            location=location,
            task_id="t",
            source_system="jll_de_newsroom",
            source_lastmod=None,
            raw_sha256=None,
        )

    assert repo.upsert_signal(make(article), run_id) == UpsertOutcome.INSERTED
    assert repo.upsert_signal(make(article), run_id) == UpsertOutcome.UNCHANGED
    changed = article.model_copy(update={"title": article.title + " (aktualisiert)"})
    assert repo.upsert_signal(make(changed), run_id) == UpsertOutcome.UPDATED
    row = conn.execute("SELECT record_version, title FROM iris_staging.signal_ingest").fetchone()
    assert row["record_version"] == 2 and row["title"].endswith("(aktualisiert)")

    assert repo.retract_signal("jll_de_newsroom", article.source_record_id, "test")
    assert not repo.retract_signal("jll_de_newsroom", article.source_record_id, "test")  # idempotent


def test_geocode_cache_roundtrip(conn: psycopg.Connection[Any]) -> None:
    cache = GeocodeCache(conn)
    cache["k"] = [{"lat": "1"}]
    assert GeocodeCache(conn)["k"] == [{"lat": "1"}]
    assert "missing" not in cache


def test_quality_report_renders(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    shutil.copytree(HTML, tmp_path / "fx")
    harvest(conn, tmp_path / "fx")
    md = render_markdown(build_report(conn))
    assert "Validation checks" in md and "❌" not in md
    assert "ambiguous" in md and "missing_body" in md


# ---------------------------------------------------------------------------------------------
# Web UI / API on top of the same database
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def client(conn: psycopg.Connection[Any], database_url: str, tmp_path: Path) -> Iterator[TestClient]:
    shutil.copytree(HTML, tmp_path / "fx")
    harvest(conn, tmp_path / "fx")
    from jll_harvester.web.app import create_app

    app = create_app(Settings(database_url=database_url, _env_file=None))  # type: ignore[call-arg]
    with TestClient(app) as c:
        yield c


def test_pages_render(client: TestClient) -> None:
    for path in [
        "/",
        "/signale",
        "/signale?typ=commercial_land_disposal&geo=resolved",
        "/karte",
        "/qualitaet",
        "/methodik",
        "/?lang=en",
    ]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "IRIS" in resp.text
    first = client.get("/api/signals?limit=1").json()["items"][0]
    detail = client.get(f"/signale/{first['signal_id']}")
    assert detail.status_code == 200 and first["title"].split()[0] in detail.text
    assert client.get("/signale/999999").status_code == 404


def test_api_and_exports(client: TestClient) -> None:
    fc = client.get("/api/signals.geojson").json()
    assert fc["type"] == "FeatureCollection" and fc["features"]
    props = fc["features"][0]["properties"]
    assert {"x_25832", "y_25832", "signal_type"} <= props.keys()
    assert client.get("/api/summary").json()["total"] == 10
    assert all(c["passed"] for c in client.get("/api/checks").json())
    csv = client.get("/export/signals.csv")
    assert csv.status_code == 200 and csv.text.startswith("signal_id,")
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and health["revision"]
    resp = client.get("/")
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_basic_auth_is_enforced_when_configured(database_url: str, conn: psycopg.Connection[Any]) -> None:
    from jll_harvester.web.app import create_app

    app = create_app(
        Settings(
            database_url=database_url, web_basic_auth_user="iris", web_basic_auth_password="s3cret", _env_file=None
        )
    )  # type: ignore[call-arg]
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("iris", "wrong")).status_code == 401
        assert c.get("/", auth=("iris", "s3cret")).status_code == 200
        assert c.get("/healthz").status_code == 200
