"""Read-side queries shared by the web UI, the quality report and the exporters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

import psycopg

SCHEMA = "iris_staging"
_CHECK_HEADER = re.compile(r"^--\s*check:\s*(?P<id>[\w-]+)\s*\|\s*(?P<severity>\w+)\s*\|\s*(?P<desc>.+)$")
SORTS = {
    "published_desc": "s.published_at DESC NULLS LAST, s.signal_id DESC",
    "published_asc": "s.published_at ASC NULLS LAST, s.signal_id",
    "confidence_desc": "s.classification_confidence DESC, s.published_at DESC NULLS LAST",
    "area_desc": "greatest(coalesce(s.area_sqm, 0), coalesce(s.plot_area_sqm, 0)) DESC, s.published_at DESC NULLS LAST",
}


@dataclass(frozen=True)
class SignalFilter:
    signal_type: str | None = None
    geocode_status: str | None = None
    q: str | None = None
    municipality: str | None = None
    year: int | None = None
    include_retracted: bool = False

    def where(self) -> tuple[str, dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if not self.include_retracted:
            clauses.append("s.retracted_at IS NULL")
        if self.signal_type:
            clauses.append("s.signal_type = %(signal_type)s")
            params["signal_type"] = self.signal_type
        if self.geocode_status:
            clauses.append("s.geocode_status = %(geocode_status)s")
            params["geocode_status"] = self.geocode_status
        if self.municipality:
            clauses.append("s.municipality = %(municipality)s")
            params["municipality"] = self.municipality
        if self.year:
            clauses.append("extract(year FROM s.published_at AT TIME ZONE 'Europe/Berlin') = %(year)s")
            params["year"] = self.year
        if self.q:
            clauses.append(
                "(s.title ILIKE %(q)s OR s.summary ILIKE %(q)s OR s.municipality ILIKE %(q)s OR s.payload->'article'->>'excerpt' ILIKE %(q)s)"
            )
            params["q"] = f"%{self.q.strip()}%"
        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


_SIGNAL_COLUMNS = """
    s.signal_id, s.source_record_id, s.source_url, s.canonical_url, s.signal_type, s.title, s.summary,
    s.published_at, s.published_at_source, s.deal_type, s.area_sqm, s.plot_area_sqm, s.property_types,
    s.source_tags, s.classification_confidence, s.location_text, s.municipality, s.district, s.street,
    s.postal_code, s.geocode_status, s.geocode_precision, s.geocode_provider, s.geocode_provider_ref,
    s.geocode_confidence, s.geocode_notes, s.quality_flags, s.record_version, s.first_seen_at,
    s.last_seen_at, s.updated_at, s.retracted_at, s.retraction_reason,
    ST_X(s.geom) AS x_25832, ST_Y(s.geom) AS y_25832,
    ST_X(ST_Transform(s.geom, 4326)) AS lon, ST_Y(ST_Transform(s.geom, 4326)) AS lat
"""


def list_signals(
    conn: psycopg.Connection[Any], flt: SignalFilter, *, sort: str = "published_desc", limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    where, params = flt.where()
    order = SORTS.get(sort, SORTS["published_desc"])
    rows = conn.execute(
        f"SELECT {_SIGNAL_COLUMNS} FROM {SCHEMA}.signal_ingest s {where} ORDER BY {order} LIMIT %(limit)s OFFSET %(offset)s",
        {**params, "limit": limit, "offset": offset},
    ).fetchall()
    total = conn.execute(f"SELECT count(*) AS n FROM {SCHEMA}.signal_ingest s {where}", params).fetchone()
    return list(rows), int(total["n"]) if total else 0


def get_signal(conn: psycopg.Connection[Any], signal_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_SIGNAL_COLUMNS}, s.classification_evidence, s.location_candidates, s.payload, s.content_hash, "
        f"s.raw_sha256, s.first_ingest_run_id, s.last_ingest_run_id, s.task_id, s.source_system, ST_AsText(s.geom) AS geom_wkt "
        f"FROM {SCHEMA}.signal_ingest s WHERE s.signal_id = %s",
        (signal_id,),
    ).fetchone()
    return dict(row) if row else None


def nearby(
    conn: psycopg.Connection[Any], signal_id: int, radius_m: int = 50_000, limit: int = 5
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT o.signal_id, o.title, o.signal_type, o.municipality, o.published_at,
               round(ST_Distance(o.geom, s.geom))::int AS distance_m
        FROM {SCHEMA}.signal_ingest s
        JOIN {SCHEMA}.signal_ingest o ON o.signal_id <> s.signal_id AND o.geom IS NOT NULL AND o.retracted_at IS NULL
             AND ST_DWithin(o.geom, s.geom, %(r)s)
        WHERE s.signal_id = %(id)s AND s.geom IS NOT NULL
        ORDER BY o.geom <-> s.geom LIMIT %(limit)s
        """,
        {"id": signal_id, "r": radius_m, "limit": limit},
    ).fetchall()
    return list(rows)


def summary(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    one = (
        conn.execute(
            f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE signal_type = 'commercial_land_disposal') AS land,
               count(*) FILTER (WHERE signal_type = 'big_box_industrial_development') AS bigbox,
               count(*) FILTER (WHERE geocode_status = 'resolved') AS resolved,
               count(*) FILTER (WHERE geocode_precision IN ('address', 'street')) AS precise,
               count(*) FILTER (WHERE geocode_status = 'ambiguous') AS ambiguous,
               count(*) FILTER (WHERE geocode_status IN ('unresolved', 'invalid')) AS unresolved,
               coalesce(sum(plot_area_sqm), 0) AS plot_sqm,
               coalesce(sum(area_sqm) FILTER (WHERE signal_type = 'big_box_industrial_development'), 0) AS bigbox_sqm,
               max(published_at) AS latest_published,
               count(DISTINCT municipality) AS municipalities
        FROM {SCHEMA}.signal_ingest WHERE retracted_at IS NULL
        """
        ).fetchone()
        or {}
    )
    by_status = conn.execute(
        f"SELECT geocode_status AS k, count(*) AS n FROM {SCHEMA}.signal_ingest WHERE retracted_at IS NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    by_precision = conn.execute(
        f"SELECT coalesce(geocode_precision, '—') AS k, count(*) AS n FROM {SCHEMA}.signal_ingest WHERE retracted_at IS NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    by_deal = conn.execute(
        f"SELECT coalesce(deal_type, 'unknown') AS k, count(*) AS n FROM {SCHEMA}.signal_ingest WHERE retracted_at IS NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    top_towns = conn.execute(
        f"SELECT municipality AS k, count(*) AS n FROM {SCHEMA}.signal_ingest WHERE retracted_at IS NULL AND municipality IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 8"
    ).fetchall()
    timeline = conn.execute(
        f"""
        SELECT to_char(date_trunc('quarter', published_at AT TIME ZONE 'Europe/Berlin'), 'YYYY-"Q"Q') AS period,
               count(*) FILTER (WHERE signal_type = 'commercial_land_disposal') AS land,
               count(*) FILTER (WHERE signal_type = 'big_box_industrial_development') AS bigbox
        FROM {SCHEMA}.signal_ingest WHERE retracted_at IS NULL AND published_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    flags = conn.execute(
        f"SELECT flag AS k, count(*) AS n FROM {SCHEMA}.signal_ingest, unnest(quality_flags) AS flag WHERE retracted_at IS NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    years = conn.execute(
        f"SELECT DISTINCT extract(year FROM published_at AT TIME ZONE 'Europe/Berlin')::int AS y FROM {SCHEMA}.signal_ingest WHERE published_at IS NOT NULL ORDER BY 1 DESC"
    ).fetchall()
    return {
        **dict(one),
        "by_status": list(by_status),
        "by_precision": list(by_precision),
        "by_deal": list(by_deal),
        "top_towns": list(top_towns),
        "timeline": list(timeline),
        "flags": list(flags),
        "years": [r["y"] for r in years],
    }


def corpus_stats(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    decisions = conn.execute(
        f"SELECT decision AS k, count(*) AS n FROM {SCHEMA}.source_document GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    reasons = conn.execute(
        f"SELECT coalesce(decision_reason, signal_type) AS k, count(*) AS n FROM {SCHEMA}.source_document GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    return {"decisions": list(decisions), "reasons": list(reasons)}


def recent_rejections(
    conn: psycopg.Connection[Any], reason: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    extra = ""
    if reason:
        extra = "AND decision_reason = %(reason)s"
        params["reason"] = reason
    rows = conn.execute(
        f"""SELECT source_record_id, url, title, published_at, decision, decision_reason,
                   classification->'evidence'->'scores' AS scores
            FROM {SCHEMA}.source_document WHERE decision <> 'signal' {extra}
            ORDER BY published_at DESC NULLS LAST LIMIT %(limit)s""",
        params,
    ).fetchall()
    return list(rows)


def runs(conn: psycopg.Connection[Any], limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""SELECT r.*, (SELECT count(*) FROM {SCHEMA}.ingest_issue i WHERE i.run_id = r.run_id AND i.severity = 'error') AS errors,
                         (SELECT count(*) FROM {SCHEMA}.ingest_issue i WHERE i.run_id = r.run_id AND i.severity = 'warning') AS warnings
            FROM {SCHEMA}.ingest_run r ORDER BY r.started_at DESC LIMIT %s""",
        (limit,),
    ).fetchall()
    return list(rows)


def latest_successful_run(conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT * FROM {SCHEMA}.ingest_run WHERE status IN ('succeeded', 'partial') ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def latest_run(conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
    found = runs(conn, 1)
    return found[0] if found else None


def run_issues(
    conn: psycopg.Connection[Any], run_id: str, severity: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"run_id": run_id, "limit": limit}
    extra = ""
    if severity:
        extra = "AND severity = %(severity)s"
        params["severity"] = severity
    rows = conn.execute(
        f"""SELECT * FROM {SCHEMA}.ingest_issue WHERE run_id = %(run_id)s {extra}
            ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, issue_id LIMIT %(limit)s""",
        params,
    ).fetchall()
    return list(rows)


def geojson(conn: psycopg.Connection[Any], flt: SignalFilter) -> dict[str, Any]:
    where, params = flt.where()
    where = f"{where} {'AND' if where else 'WHERE'} s.geom IS NOT NULL"
    rows = conn.execute(
        f"""SELECT s.signal_id, s.signal_type, s.title, s.published_at, s.municipality, s.district, s.street,
                   s.geocode_precision, s.area_sqm, s.plot_area_sqm, s.deal_type, s.source_url,
                   s.classification_confidence,
                   ST_X(s.geom) AS x_25832, ST_Y(s.geom) AS y_25832,
                   ST_AsGeoJSON(ST_Transform(s.geom, 4326), 6)::json AS geometry
            FROM {SCHEMA}.signal_ingest s {where} ORDER BY s.published_at DESC NULLS LAST""",
        params,
    ).fetchall()
    features = []
    for r in rows:
        props = {k: v for k, v in r.items() if k != "geometry"}
        props["published_at"] = r["published_at"].isoformat() if r["published_at"] else None
        for k in ("area_sqm", "plot_area_sqm", "classification_confidence"):
            props[k] = float(props[k]) if props[k] is not None else None
        features.append({"type": "Feature", "id": r["signal_id"], "geometry": r["geometry"], "properties": props})
    return {
        "type": "FeatureCollection",
        "name": "iris_sig45_signals",
        "crs_note": "geometry in WGS84 (RFC 7946); x_25832/y_25832 hold the staged EPSG:25832 coordinates",
        "features": features,
    }


@dataclass
class CheckResult:
    id: str
    severity: str
    description: str
    sql: str
    value: int | None
    passed: bool
    error: str | None = None


def load_checks() -> list[tuple[str, str, str, str]]:
    text = (resources.files("jll_harvester") / "db" / "validation.sql").read_text(encoding="utf-8")
    checks: list[tuple[str, str, str, str]] = []
    current: tuple[str, str, str] | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _CHECK_HEADER.match(line.strip())
        if m:
            if current and buf:
                checks.append((*current, "\n".join(buf).strip()))
            current, buf = (m["id"], m["severity"], m["desc"].strip()), []
        elif current and not line.strip().startswith("--"):
            buf.append(line)
    if current and buf:
        checks.append((*current, "\n".join(buf).strip()))
    return checks


def run_checks(conn: psycopg.Connection[Any]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check_id, severity, desc, sql in load_checks():
        try:
            with conn.transaction():
                row = conn.execute(sql.rstrip(";")).fetchone()
            value = int(next(iter(row.values()))) if row else None
            is_violation_check = "violations" in (row or {})
            passed = (value == 0) if is_violation_check else True
            results.append(CheckResult(check_id, severity, desc, sql, value, passed))
        except psycopg.Error as exc:
            results.append(CheckResult(check_id, severity, desc, sql, None, False, str(exc).strip()))
    return results
