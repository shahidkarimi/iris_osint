-- IRIS-SIG-45 staging schema — PostgreSQL 16 / PostGIS 3.4
-- Idempotent: safe to run repeatedly. Applied by `jll-harvester migrate`.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS iris_staging;

-- ---------------------------------------------------------------------------------------------
-- Run ledger: one row per harvest execution
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.ingest_run (
    run_id              uuid PRIMARY KEY,
    task_id             text        NOT NULL,
    source_system       text        NOT NULL,
    mode                text        NOT NULL,             -- live | fixture | reprocess
    started_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz,
    status              text        NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    source_check        jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- brief endpoint redirect, robots
    stats               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    config              jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error               text
);

-- ---------------------------------------------------------------------------------------------
-- Crawl ledger: every discovered source document and what happened to it
-- (drives incremental runs via sitemap <lastmod> and keeps rejections traceable)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.source_document (
    source_system       text        NOT NULL,
    source_record_id    text        NOT NULL,
    url                 text        NOT NULL,
    sitemap_lastmod     timestamptz,
    fetched_at          timestamptz,
    http_status         integer,
    raw_sha256          text,
    title               text,
    published_at        timestamptz,
    decision            text        NOT NULL
                        CHECK (decision IN ('signal', 'rejected', 'fetch_error', 'parse_error', 'skipped')),
    decision_reason     text,
    signal_type         text,
    classification      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    last_run_id         uuid        REFERENCES iris_staging.ingest_run (run_id),
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, source_record_id)
);

-- ---------------------------------------------------------------------------------------------
-- The staging target named in the brief
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest (
    signal_id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id                    text        NOT NULL,
    source_system              text        NOT NULL,
    source_record_id           text        NOT NULL,
    source_url                 text        NOT NULL CHECK (source_url ~ '^https?://'),
    canonical_url              text        NOT NULL,
    signal_type                text        NOT NULL
                               CHECK (signal_type IN ('commercial_land_disposal', 'big_box_industrial_development')),
    title                      text        NOT NULL CHECK (length(btrim(title)) > 0),
    summary                    text,
    published_at               timestamptz,
    published_at_source        text CHECK (published_at_source IN ('json-ld', 'dateline')),
    source_lastmod             timestamptz,
    language                   text,
    deal_type                  text CHECK (deal_type IN ('acquisition', 'disposal', 'letting', 'development')),
    area_sqm                   numeric(14, 2) CHECK (area_sqm IS NULL OR area_sqm > 0),
    plot_area_sqm              numeric(14, 2) CHECK (plot_area_sqm IS NULL OR plot_area_sqm > 0),
    property_types             text[]      NOT NULL DEFAULT '{}',
    source_tags                text[]      NOT NULL DEFAULT '{}',
    classification_confidence  numeric(4, 3) NOT NULL CHECK (classification_confidence BETWEEN 0 AND 1),
    classification_evidence    jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- geography
    country_code               char(2)     NOT NULL DEFAULT 'DE',
    location_text              text,
    municipality               text,
    district                   text,
    street                     text,
    postal_code                text CHECK (postal_code IS NULL OR postal_code ~ '^[0-9]{5}$'),
    geocode_status             text        NOT NULL
                               CHECK (geocode_status IN ('resolved', 'ambiguous', 'unresolved', 'invalid')),
    geocode_precision          text CHECK (geocode_precision IN ('address', 'street', 'district', 'municipality')),
    geocode_provider           text,
    geocode_provider_ref       text,
    geocode_confidence         numeric(4, 3),
    geocode_notes              text[]      NOT NULL DEFAULT '{}',
    location_candidates        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    geom                       geometry(Point, 25832),

    -- quality & lineage
    quality_flags              text[]      NOT NULL DEFAULT '{}',
    raw_sha256                 text,
    content_hash               text        NOT NULL,
    record_version             integer     NOT NULL DEFAULT 1,
    payload                    jsonb       NOT NULL,
    first_ingest_run_id        uuid        REFERENCES iris_staging.ingest_run (run_id),
    last_ingest_run_id         uuid        REFERENCES iris_staging.ingest_run (run_id),
    first_seen_at              timestamptz NOT NULL DEFAULT now(),
    last_seen_at               timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    -- set when a later run no longer classifies the source as a signal (kept for lineage)
    retracted_at               timestamptz,
    retraction_reason          text,

    CONSTRAINT signal_ingest_natural_key UNIQUE (source_system, source_record_id),
    -- a point exists if and only if the location is resolved
    CONSTRAINT signal_ingest_geom_matches_status CHECK (
        (geocode_status = 'resolved') = (geom IS NOT NULL)
    ),
    CONSTRAINT signal_ingest_precision_when_resolved CHECK (
        geocode_status <> 'resolved' OR geocode_precision IS NOT NULL
    ),
    -- invalid coordinates are never accepted silently: points must be valid and inside Germany
    CONSTRAINT signal_ingest_geom_valid CHECK (
        geom IS NULL OR (
            ST_IsValid(geom)
            AND ST_X(geom) BETWEEN 200000 AND 960000
            AND ST_Y(geom) BETWEEN 5200000 AND 6120000
        )
    )
);

CREATE INDEX IF NOT EXISTS signal_ingest_geom_gix       ON iris_staging.signal_ingest USING gist (geom);
CREATE INDEX IF NOT EXISTS signal_ingest_type_pub_idx   ON iris_staging.signal_ingest (signal_type, published_at DESC);
CREATE INDEX IF NOT EXISTS signal_ingest_status_idx     ON iris_staging.signal_ingest (geocode_status);
CREATE INDEX IF NOT EXISTS signal_ingest_tags_gin       ON iris_staging.signal_ingest USING gin (source_tags);
CREATE INDEX IF NOT EXISTS source_document_decision_idx ON iris_staging.source_document (decision);

-- ---------------------------------------------------------------------------------------------
-- Issues: errors, warnings and data-quality findings per run (the "error / quality report")
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.ingest_issue (
    issue_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              uuid        NOT NULL REFERENCES iris_staging.ingest_run (run_id) ON DELETE CASCADE,
    severity            text        NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    stage               text        NOT NULL,
    code                text        NOT NULL,
    message             text        NOT NULL,
    source_url          text,
    details             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_issue_run_idx ON iris_staging.ingest_issue (run_id, severity);

-- ---------------------------------------------------------------------------------------------
-- Geocoder response cache (Nominatim usage policy: cache, don't repeat queries)
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.geocode_cache (
    cache_key           text PRIMARY KEY,
    response            jsonb       NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------------------------
-- Convenience view for analysts / GIS clients (WGS84 for web maps)
-- ---------------------------------------------------------------------------------------------
CREATE OR REPLACE VIEW iris_staging.v_signal_map AS
SELECT
    s.signal_id,
    s.signal_type,
    s.title,
    s.published_at,
    s.municipality,
    s.district,
    s.street,
    s.geocode_precision,
    s.area_sqm,
    s.plot_area_sqm,
    s.deal_type,
    s.source_url,
    ST_X(s.geom) AS x_25832,
    ST_Y(s.geom) AS y_25832,
    ST_Transform(s.geom, 4326) AS geom_4326
FROM iris_staging.signal_ingest s
WHERE s.geom IS NOT NULL
  AND s.retracted_at IS NULL;

CREATE TABLE IF NOT EXISTS iris_staging.schema_migration (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
