"""PostgreSQL / PostGIS persistence (psycopg 3).

Idempotency contract for ``iris_staging.signal_ingest``:

* natural key ``(source_system, source_record_id)`` is UNIQUE — a re-run can never duplicate;
* ``content_hash`` covers the normalised business fields only (not fetch timestamps or the raw
  HTML hash, which changes with tracking scripts), so an unchanged article is ``unchanged`` and
  only ``last_seen_at`` / ``last_ingest_run_id`` move;
* a changed article is updated in place and ``record_version`` is incremented;
* ``INSERT … ON CONFLICT DO NOTHING`` + row lock makes concurrent writers safe, and a
  transaction-scoped advisory lock prevents two harvests from running at once.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, MutableMapping
from datetime import datetime
from enum import StrEnum
from importlib import resources
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..models import Issue, Signal

log = logging.getLogger(__name__)

SCHEMA = "iris_staging"
_HARVEST_LOCK_KEY = 4_510_045  # arbitrary, stable advisory-lock id for IRIS-SIG-45


class UpsertOutcome(StrEnum):
    INSERTED = "inserted"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


def connect(dsn: str, *, autocommit: bool = False) -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row, connect_timeout=10)


def migrate(conn: psycopg.Connection[Any]) -> list[str]:
    """Apply bundled SQL migrations in order. Returns the versions applied by this call."""
    files = sorted(
        (f for f in (resources.files("jll_harvester") / "db" / "migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_HARVEST_LOCK_KEY + 1,))
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_migration "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {
            r[0] if isinstance(r, tuple) else r["version"]
            for r in conn.execute(f"SELECT version FROM {SCHEMA}.schema_migration")
        }
        for f in files:
            version = f.name.removesuffix(".sql")
            if version in done:
                continue
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute(f"INSERT INTO {SCHEMA}.schema_migration (version) VALUES (%s)", (version,))
            applied.append(version)
    return applied


class GeocodeCache(MutableMapping[str, Any]):
    """DB-backed cache so repeated runs never re-query Nominatim for the same address."""

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn
        self._local: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        if key in self._local:
            return self._local[key]
        row = self._conn.execute(f"SELECT response FROM {SCHEMA}.geocode_cache WHERE cache_key = %s", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        value = row["response"] if isinstance(row, dict) else row[0]
        self._local[key] = value
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self._local[key] = value
        self._conn.execute(
            f"INSERT INTO {SCHEMA}.geocode_cache (cache_key, response) VALUES (%s, %s) "
            "ON CONFLICT (cache_key) DO UPDATE SET response = EXCLUDED.response, created_at = now()",
            (key, Jsonb(value)),
        )

    def __delitem__(self, key: str) -> None:
        self._local.pop(key, None)
        self._conn.execute(f"DELETE FROM {SCHEMA}.geocode_cache WHERE cache_key = %s", (key,))

    def __iter__(self) -> Iterator[str]:
        return iter(self._local)

    def __len__(self) -> int:
        return len(self._local)

    def __contains__(self, key: object) -> bool:
        try:
            self[str(key)]
        except KeyError:
            return False
        return True


class HarvestAlreadyRunning(RuntimeError):
    pass


class Repository:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn

    # -- run ledger ------------------------------------------------------------------------
    def acquire_harvest_lock(self) -> None:
        row = self.conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (_HARVEST_LOCK_KEY,)).fetchone()
        if not row or not (row["ok"] if isinstance(row, dict) else row[0]):
            raise HarvestAlreadyRunning("another harvest holds the advisory lock")

    def release_harvest_lock(self) -> None:
        self.conn.execute("SELECT pg_advisory_unlock(%s)", (_HARVEST_LOCK_KEY,))

    def start_run(self, *, task_id: str, source_system: str, mode: str, config: dict[str, Any]) -> uuid.UUID:
        run_id = uuid.uuid4()
        self.conn.execute(
            f"INSERT INTO {SCHEMA}.ingest_run (run_id, task_id, source_system, mode, config) VALUES (%s, %s, %s, %s, %s)",
            (run_id, task_id, source_system, mode, Jsonb(config)),
        )
        self.conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        stats: dict[str, Any],
        source_check: dict[str, Any],
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            f"UPDATE {SCHEMA}.ingest_run SET finished_at = now(), status = %s, stats = %s, source_check = %s, error = %s WHERE run_id = %s",
            (status, Jsonb(stats), Jsonb(source_check), error, run_id),
        )
        self.conn.commit()

    def record_source_check(self, run_id: uuid.UUID, source_check: dict[str, Any]) -> None:
        """Persist the source verification as soon as it is known (visible while the run is in progress)."""
        self.conn.execute(
            f"UPDATE {SCHEMA}.ingest_run SET source_check = %s WHERE run_id = %s", (Jsonb(source_check), run_id)
        )
        self.conn.commit()

    def add_issues(self, run_id: uuid.UUID, issues: list[Issue]) -> None:
        if not issues:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {SCHEMA}.ingest_issue (run_id, severity, stage, code, message, source_url, details) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (run_id, i.severity.value, i.stage, i.code, i.message[:2000], i.source_url, Jsonb(i.details))
                    for i in issues
                ],
            )

    # -- crawl ledger ----------------------------------------------------------------------
    def known_documents(self, source_system: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT source_record_id, sitemap_lastmod, decision FROM {SCHEMA}.source_document WHERE source_system = %s",
            (source_system,),
        ).fetchall()
        return {r["source_record_id"]: r for r in rows}

    def snapshot_documents(self, source_system: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT url, sitemap_lastmod, raw_sha256 FROM {SCHEMA}.source_document "
            "WHERE source_system = %s AND raw_sha256 IS NOT NULL ORDER BY sitemap_lastmod DESC NULLS LAST",
            (source_system,),
        ).fetchall()
        return list(rows)

    def upsert_document(
        self,
        *,
        run_id: uuid.UUID,
        source_system: str,
        source_record_id: str,
        url: str,
        sitemap_lastmod: datetime | None,
        decision: str,
        decision_reason: str | None = None,
        fetched_at: datetime | None = None,
        http_status: int | None = None,
        raw_sha256: str | None = None,
        title: str | None = None,
        published_at: datetime | None = None,
        signal_type: str | None = None,
        classification: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            f"""
            INSERT INTO {SCHEMA}.source_document AS d (
                source_system, source_record_id, url, sitemap_lastmod, fetched_at, http_status, raw_sha256,
                title, published_at, decision, decision_reason, signal_type, classification, last_run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_system, source_record_id) DO UPDATE SET
                url = EXCLUDED.url,
                sitemap_lastmod = EXCLUDED.sitemap_lastmod,
                fetched_at = COALESCE(EXCLUDED.fetched_at, d.fetched_at),
                http_status = COALESCE(EXCLUDED.http_status, d.http_status),
                raw_sha256 = COALESCE(EXCLUDED.raw_sha256, d.raw_sha256),
                title = COALESCE(EXCLUDED.title, d.title),
                published_at = COALESCE(EXCLUDED.published_at, d.published_at),
                decision = EXCLUDED.decision,
                decision_reason = EXCLUDED.decision_reason,
                signal_type = EXCLUDED.signal_type,
                classification = EXCLUDED.classification,
                last_run_id = EXCLUDED.last_run_id,
                last_seen_at = now()
            """,
            (
                source_system,
                source_record_id,
                url,
                sitemap_lastmod,
                fetched_at,
                http_status,
                raw_sha256,
                title,
                published_at,
                decision,
                decision_reason,
                signal_type,
                Jsonb(classification or {}),
                run_id,
            ),
        )

    # -- signals ---------------------------------------------------------------------------
    def upsert_signal(self, signal: Signal, run_id: uuid.UUID) -> UpsertOutcome:
        loc = signal.location
        has_point = loc.x_25832 is not None and loc.y_25832 is not None
        values: dict[str, Any] = {
            "task_id": signal.task_id,
            "source_system": signal.source_system,
            "source_record_id": signal.source_record_id,
            "source_url": signal.source_url,
            "canonical_url": signal.canonical_url,
            "signal_type": signal.signal_type.value,
            "title": signal.title,
            "summary": signal.summary,
            "published_at": signal.published_at,
            "published_at_source": signal.published_at_source,
            "source_lastmod": signal.source_lastmod,
            "language": signal.language,
            "deal_type": signal.deal_type,
            "area_sqm": signal.area_sqm,
            "plot_area_sqm": signal.plot_area_sqm,
            "property_types": signal.property_types,
            "source_tags": signal.source_tags,
            "classification_confidence": signal.classification_confidence,
            "classification_evidence": Jsonb(signal.classification_evidence),
            "country_code": loc.country_code,
            "location_text": loc.location_text,
            "municipality": loc.municipality,
            "district": loc.district,
            "street": loc.street,
            "postal_code": loc.postal_code,
            "geocode_status": loc.status.value,
            "geocode_precision": loc.precision.value if loc.precision else None,
            "geocode_provider": loc.provider,
            "geocode_provider_ref": loc.provider_ref,
            "geocode_confidence": loc.confidence,
            "geocode_notes": loc.notes,
            "location_candidates": Jsonb(loc.candidates),
            "quality_flags": signal.quality_flags,
            "raw_sha256": signal.raw_sha256,
            "content_hash": signal.content_hash,
            "payload": Jsonb(signal.payload),
            "x": loc.x_25832 if has_point else None,
            "y": loc.y_25832 if has_point else None,
            "run_id": run_id,
        }
        columns = [k for k in values if k not in {"x", "y", "run_id"}]
        geom_sql = "CASE WHEN %(x)s::float8 IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%(x)s::float8, %(y)s::float8), 25832) END"

        inserted = self.conn.execute(
            f"""
            INSERT INTO {SCHEMA}.signal_ingest ({", ".join(columns)}, geom, first_ingest_run_id, last_ingest_run_id)
            VALUES ({", ".join(f"%({c})s" for c in columns)}, {geom_sql}, %(run_id)s, %(run_id)s)
            ON CONFLICT (source_system, source_record_id) DO NOTHING
            RETURNING signal_id
            """,
            values,
        ).fetchone()
        if inserted:
            return UpsertOutcome.INSERTED

        current = self.conn.execute(
            f"SELECT content_hash, retracted_at FROM {SCHEMA}.signal_ingest "
            "WHERE source_system = %(source_system)s AND source_record_id = %(source_record_id)s FOR UPDATE",
            values,
        ).fetchone()
        assert current is not None
        if current["content_hash"] == signal.content_hash and current["retracted_at"] is None:
            self.conn.execute(
                f"UPDATE {SCHEMA}.signal_ingest SET last_seen_at = now(), last_ingest_run_id = %(run_id)s "
                "WHERE source_system = %(source_system)s AND source_record_id = %(source_record_id)s",
                values,
            )
            return UpsertOutcome.UNCHANGED

        assignments = ", ".join(f"{c} = %({c})s" for c in columns if c not in {"source_system", "source_record_id"})
        self.conn.execute(
            f"""
            UPDATE {SCHEMA}.signal_ingest SET {assignments},
                geom = {geom_sql},
                record_version = record_version + 1,
                last_ingest_run_id = %(run_id)s,
                last_seen_at = now(),
                updated_at = now(),
                retracted_at = NULL,
                retraction_reason = NULL
            WHERE source_system = %(source_system)s AND source_record_id = %(source_record_id)s
            """,
            values,
        )
        return UpsertOutcome.UPDATED

    def retract_signal(self, source_system: str, source_record_id: str, reason: str) -> bool:
        row = self.conn.execute(
            f"UPDATE {SCHEMA}.signal_ingest SET retracted_at = now(), retraction_reason = %s, updated_at = now() "
            "WHERE source_system = %s AND source_record_id = %s AND retracted_at IS NULL RETURNING signal_id",
            (reason, source_system, source_record_id),
        ).fetchone()
        return row is not None

    def count_signals(self) -> int:
        row = self.conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.signal_ingest").fetchone()
        return int(row["n"]) if row else 0


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
