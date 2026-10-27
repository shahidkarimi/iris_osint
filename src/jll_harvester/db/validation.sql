-- IRIS-SIG-45 — validation queries for iris_staging.signal_ingest
--
-- Run by hand:   psql "$IRIS_DATABASE_URL" -f src/jll_harvester/db/validation.sql
-- Run by the app: `jll-harvester validate` and the "Datenqualität" page parse the `-- check:` headers.
--
-- Convention: every check returns ONE row with a column `violations` (expected 0) or `value`
-- (informational). Header format:  -- check: <id> | <severity> | <description>

-- check: duplicate_natural_keys | error | No source record appears twice (re-runs must not duplicate)
SELECT count(*) AS violations FROM (
    SELECT source_system, source_record_id FROM iris_staging.signal_ingest
    GROUP BY 1, 2 HAVING count(*) > 1
) d;

-- check: duplicate_source_urls | error | No canonical URL is loaded twice under different ids
SELECT count(*) AS violations FROM (
    SELECT lower(canonical_url) FROM iris_staging.signal_ingest
    WHERE retracted_at IS NULL GROUP BY 1 HAVING count(*) > 1
) d;

-- check: geom_srid | error | Every geometry is a point in EPSG:25832
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL AND (ST_SRID(geom) <> 25832 OR GeometryType(geom) <> 'POINT');

-- check: geom_invalid_or_outside_germany | error | Points are valid and fall inside Germany (generous buffer)
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL AND (
    NOT ST_IsValid(geom)
    OR NOT ST_Intersects(
        ST_Transform(geom, 4326),
        ST_MakeEnvelope(5.5, 47.0, 15.6, 55.2, 4326)
    )
);

-- check: geom_status_consistency | error | A point exists iff geocode_status = 'resolved'
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE (geocode_status = 'resolved') <> (geom IS NOT NULL);

-- check: projection_roundtrip | error | Stored point matches the WGS84 coordinate in the payload (< 1 m)
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
  AND ST_Distance(
        geom,
        ST_Transform(ST_SetSRID(ST_MakePoint(
            (payload -> 'location' ->> 'lon')::float8,
            (payload -> 'location' ->> 'lat')::float8), 4326), 25832)
      ) > 1.0;

-- check: missing_title_or_url | error | Mandatory lineage fields are populated
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE coalesce(btrim(title), '') = '' OR source_url !~ '^https?://' OR coalesce(content_hash, '') = '';

-- check: signal_type_domain | error | Only the two in-scope signal types are present
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE signal_type NOT IN ('commercial_land_disposal', 'big_box_industrial_development');

-- check: published_in_future | warning | No publication date lies in the future
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE published_at > now() + interval '1 day';

-- check: missing_published_at | warning | Records without a verified publication date
SELECT count(*) AS violations FROM iris_staging.signal_ingest
WHERE published_at IS NULL AND retracted_at IS NULL;

-- check: unresolved_locations | info | Records without a point (ambiguous / unresolved / invalid)
SELECT count(*) AS value FROM iris_staging.signal_ingest
WHERE geocode_status <> 'resolved' AND retracted_at IS NULL;

-- check: low_confidence | info | Records with classification confidence below 0.65
SELECT count(*) AS value FROM iris_staging.signal_ingest
WHERE classification_confidence < 0.65 AND retracted_at IS NULL;

-- check: retracted | info | Records retracted by a later run (kept for lineage)
SELECT count(*) AS value FROM iris_staging.signal_ingest WHERE retracted_at IS NOT NULL;

-- check: orphan_documents | warning | Signals without a matching crawl-ledger entry
SELECT count(*) AS violations FROM iris_staging.signal_ingest s
LEFT JOIN iris_staging.source_document d USING (source_system, source_record_id)
WHERE d.source_record_id IS NULL;
