# Sample output (public source)

Produced from the live harvest of JLL Germany's newsroom into `iris_staging.signal_ingest`:

| File | Content |
|---|---|
| `signals.geojson` | staged signals with a validated point — geometry in WGS84 (RFC 7946), staged EPSG:25832 coordinates in `x_25832` / `y_25832` |
| `signals.csv` | all staged signals incl. ambiguous / unresolved ones (empty coordinates), quality flags, source URLs |
| `signals.jsonl` | the same rows as JSON lines |
| `quality_report.md` | concise error & data-quality report: run statistics, source verification, validation checks, records without a point, issues |

Regenerate:

```bash
docker compose run --rm harvester export --out-dir samples
docker compose run --rm harvester report --out samples/quality_report.md
```

All content derives from publicly accessible JLL press releases; no personal data beyond names of companies
and the JLL press contacts named as article authors in the source's own metadata (not exported here).
