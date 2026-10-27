# IRIS-SIG-45 — Error & data-quality report

Generated 2026-10-26T20:04:18+00:00 · source `jll_de_newsroom` · target `iris_staging.signal_ingest`

## Reported run

| run_id | mode | status | started | duration (s) | errors | warnings |
|---|---|---|---|---|---|---|
| a3ceebe5 | live | succeeded | 2026-10-26T19:38:54+00:00 | 1060.8 | 0 | 22 |

Counters: `discovered`=701, `fetched`=701, `rejected`=678, `signal_inserted`=23

**Source verification** (performed at run time, not assumed):

- brief endpoint `https://www.jll.de/de/trends-and-insights/research`: 301 https://www.jll.de/de/trends-and-insights/research → **200** `https://www.jll.com/de-de/insights/market-dynamics`
- robots.txt forbids query strings (listing pagination): **True**; sitemap allowed: **True**

## Run history

| run_id | mode | status | started | duration (s) | counters | error |
|---|---|---|---|---|---|---|
| 2bfb022f | reprocess | succeeded | 2026-10-26T20:02:56+00:00 | 80.8 | discovered=701, fetched=701, rejected=678, signal_unchanged=22, signal_updated=1 |  |
| f45da3e9 | live | failed | 2026-10-26T20:01:43+00:00 | 1.6 |  | DiscoveryError: access_denied: access_denied: HTTP 403 from AkamaiGHost — access restriction respected, not by |
| 677e6078 | live | failed | 2026-10-26T20:01:41+00:00 | 1.9 |  | DiscoveryError: access_denied: access_denied: HTTP 403 from AkamaiGHost — access restriction respected, not by |
| 24c8c7d2 | live | succeeded | 2026-10-26T20:00:45+00:00 | 2.6 | discovered=701, skipped_unchanged=701 |  |
| db63107d | reprocess | succeeded | 2026-10-26T19:59:10+00:00 | 79.1 | discovered=701, fetched=701, rejected=678, signal_unchanged=21, signal_updated=2 |  |
| a3ceebe5 | live | succeeded | 2026-10-26T19:38:54+00:00 | 1060.8 | discovered=701, fetched=701, rejected=678, signal_inserted=23 |  |
| cf903932 | live | failed | 2026-10-26T19:37:27+00:00 | 1.6 |  | DiscoveryError: http_403: http_403: HTTP 403 (https://www.jll.com/de-de/sitemap.xml) (https://www.jll.com/de-d |

## Staged signals

| total | land disposals | big-box industrial | with point | address/street precision | ambiguous | unresolved/invalid | municipalities |
|---|---|---|---|---|---|---|---|
| 23 | 8 | 15 | 22 | 9 | 1 | 0 | 20 |

Geocode precision: `municipality` 10, `address` 8, `district` 3, `—` 1, `street` 1

## Corpus decisions (crawl ledger)

| decision / reason | documents |
|---|---|
| market_research | 418 |
| out_of_domain:office | 60 |
| personnel_news | 60 |
| out_of_domain:retail | 41 |
| not_a_single_deal | 36 |
| below_confidence_threshold | 34 |
| big_box_industrial_development | 15 |
| out_of_domain:unknown | 11 |
| commercial_land_disposal | 8 |
| out_of_domain:living-and-multifamily | 6 |
| out_of_domain:office,retail | 4 |
| out_of_domain:land | 2 |
| out_of_domain:coworking-and-flex | 2 |
| out_of_domain:healthcare-property | 2 |
| out_of_domain:lab | 1 |
| out_of_domain:healthcare-property,office | 1 |

## Validation checks

| check | severity | result | value | description |
|---|---|---|---|---|
| duplicate_natural_keys | error | ✅ pass | 0 | No source record appears twice (re-runs must not duplicate) |
| duplicate_source_urls | error | ✅ pass | 0 | No canonical URL is loaded twice under different ids |
| geom_srid | error | ✅ pass | 0 | Every geometry is a point in EPSG:25832 |
| geom_invalid_or_outside_germany | error | ✅ pass | 0 | Points are valid and fall inside Germany (generous buffer) |
| geom_status_consistency | error | ✅ pass | 0 | A point exists iff geocode_status = 'resolved' |
| projection_roundtrip | error | ✅ pass | 0 | Stored point matches the WGS84 coordinate in the payload (< 1 m) |
| missing_title_or_url | error | ✅ pass | 0 | Mandatory lineage fields are populated |
| signal_type_domain | error | ✅ pass | 0 | Only the two in-scope signal types are present |
| published_in_future | warning | ✅ pass | 0 | No publication date lies in the future |
| missing_published_at | warning | ✅ pass | 0 | Records without a verified publication date |
| unresolved_locations | info | ✅ pass | 1 | Records without a point (ambiguous / unresolved / invalid) |
| low_confidence | info | ✅ pass | 1 | Records with classification confidence below 0.65 |
| retracted | info | ✅ pass | 0 | Records retracted by a later run (kept for lineage) |
| orphan_documents | warning | ✅ pass | 0 | Signals without a matching crawl-ledger entry |

## Quality flags on staged records

| flag | records |
|---|---|
| location_coarse_municipality | 10 |
| location_precision_downgraded | 1 |
| published_at_from_dateline | 1 |
| low_classification_confidence | 1 |
| date_mismatch_jsonld_vs_dateline_366d | 1 |
| location_near_reference_town | 1 |
| location_ambiguous | 1 |

## Records without a point (documented, not silently dropped)

| id | status | location text | notes | title |
|---|---|---|---|---|
| 7 | ambiguous | Geislingen an der Steige / Epfendorf | several equally plausible locations: Geislingen an der Steige, Epfendorf | EQT kauft Logistikportfolio „Southgates“ mit insgesamt 50.000 m² |

## Issues raised in the reported run

| severity | stage | code | message |
|---|---|---|---|
| warning | parse | json_ld_malformed | json_ld_malformed: Positive Aussichten stabilisieren Finanzierungsindex Difi |
| warning | parse | json_ld_malformed | json_ld_malformed: Investoren setzen auf Lebensmittelhandel und Fachmarktzentren |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_12d: HypoVereinsbank mietet rund 700 m² in der Kölner Innenstadt |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_35d: Defizit beim Wohnungsbau fällt höher aus als bisher angenommen |
| warning | parse | json_ld_malformed | json_ld_malformed: Victor Prime Office tritt zum Jahresbeginn 2026 auf der Stelle |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_91d: Viel Rückenwind für Hannovers Bürovermietungsmarkt |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_91d: Nürnbergs Büromarkt profitiert von größeren Anmietungen |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_31d: Investmentmarkt für Gesundheitsimmobilien startet mit kräftigem Umsatzplus |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_91d: Hamburgs Büromarkt hadert mit der Kleinteiligkeit |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_91d: Düsseldorfs Büromarkt hat Klasse, aber es fehlt die Masse |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_91d: Kölns Büromarkt kommt im zweiten Quartal kaum vom Fleck |
| warning | geocode | location_ambiguous | EQT kauft Logistikportfolio „Southgates“ mit insgesamt 50.000 m² — several equally plausible locations: Geislingen an der Steige, Epfendorf |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_6d: Logistics Solution mietet 3.500 m² am Berliner Flughafen BER |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_365d: Paref Gestion verkauft für seine SCPI Paref Prima ein Fachmarktzentrum im südhessischen Bürstadt |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_3d: Hoher Flächenumsatz am Frankfurter Büromarkt |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_3d: Spitzenmiete für Münchner Büros knackt 60-Euro-Marke |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_366d: Logistikzentrum des Versandhauses Klingel in Pforzheim wechselt Besitzer |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_3d: Immobilienfinanzierer lassen Raum für alternative Kreditgeber |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_20d: Fitnesskette Evo zieht auf ehemalige Fläche der Berliner Volksbank |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_4d: Investoren machen einen Bogen um Pflegeheime und Kliniken |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_61d: Rebecca Froehlich leitet das Retail Investment Team Nord bei JLL |
| warning | parse | date_mismatch | date_mismatch_jsonld_vs_dateline_365d: Heidi Schmidtke übernimmt Gesamtleitung der Hotelsparte von JLL Deutschland |
