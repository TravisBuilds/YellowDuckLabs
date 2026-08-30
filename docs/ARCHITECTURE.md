# Architecture, and where it departs from the brief

The build brief is followed closely. This document records the places it is not,
and why, so that each deviation is a decision on the record rather than a
discrepancy someone discovers later.

---

## Deviations from the brief

### 1. One `features` table, not a table per feature type

**The brief suggests** separate tables: `buildings`, `roads`, `water_assets`,
`fire_stations`, and so on.

**The implementation uses** a single `features` table with a `feature_kind`
discriminator, plus SQL views (`buildings`, `roads`, ...) that filter it, so
queries written against the brief's shape still work.

**Why.** Feature kinds are configuration, not schema. A municipality that
publishes something West Vancouver does not — fuel treatment polygons, evacuation
routes, cisterns — should be onboardable by adding a source to a YAML file. With
a table per kind, every new kind is a migration, and the portability claim
quietly becomes false. The generic table also lets precedence resolution be one
piece of logic operating on one table rather than a switch across many.

**Cost.** No per-kind column typing; kind-specific attributes live in a JSONB
`properties` column. Acceptable, because the analysis reads geometry and a
handful of well-known fields, and provenance is uniform across kinds.

### 2. Geometric mean rather than a literal five-way product

**The brief specifies** `priority = ignition × spread × consequence ×
observation_gap × access`.

**The implementation uses** the geometric mean, which is that product raised to
1/5.

**Why.** Five values in 0–1 multiplied together land almost everything near zero:
five components at a genuinely serious 0.7 produce 0.168, which reads as
negligible. The geometric mean preserves the ordering exactly, preserves the
important property that one low component suppresses the whole score, and keeps
the result on a scale a person can reason about.

**Disclosure.** The score payload carries `formula_note` stating the deviation
and that the formula is an unvalidated working hypothesis. It is shown in the UI,
not buried in a config.

### 3. Line-of-sight visibility instead of a detection-recency proxy

**The brief describes** the observation gap in terms of satellite revisit and
detection recency.

**The implementation makes** line-of-sight from the road network the dominant
term (weight 0.55), through terrain and typical FBP canopy height, with
detection recency reduced to 0.10.

**Why.** Satellite revisit is nearly uniform across a municipality 87 km²
across, so a component built on it ranks nothing — every cell scored almost
identically and the component was saturated. Terrain visibility varies enormously
over the same area and is the thing that actually determines whether a fire grows
unseen. See the README section on the observation gap.

**Cost.** Slower derivation: 19,471 observer points against 15,798 cells. It is
vectorised per cell and takes a few minutes.

### 4. No GDAL, geopandas or rasterio

**Why.** `shapely` and `pyproj` cover the vector work, PostGIS does the spatial
SQL, and the DEM arrives as Terrarium-encoded PNG tiles that Pillow and numpy
decode directly. This keeps the image free of a GDAL toolchain, which is the
single largest source of build fragility in Python geospatial work.

**Cost.** No raster reprojection or format zoo. The DEM is a fixed-schema PNG
tile set; the CWFIS FBP fuel grid is a GeoTIFF whose affine is read from the
ModelTransformation tag. Both are decoded with Pillow and numpy.

### 5. Terrarium tiles rather than a Canadian DEM

**Why portability.** Terrarium is global, keyless and needs no per-municipality
configuration, so terrain derivation works the moment a boundary is known. NRCan
CDEM and municipal LiDAR are both better for West Vancouver specifically, and
substituting the District's bare-earth LiDAR would materially improve slope,
ruggedness and every sight line. This is recorded as a caveat on the source and
as a known gap in the README.

---

## Data flow

```
municipality YAML
      |
      v
  boundary fetch  ->  Municipality row
      |
      v
  source adapters (discover -> fetch -> normalize -> validate -> clip)
      |
      +-> Dataset / DatasetVersion   (status, licence, attribution, message)
      +-> Feature rows              (geometry, observed_at, properties)
      |
      v
  precedence resolution  (municipal > provincial > federal > remote > community)
      |
      v
  H3 grid generation  ->  AnalysisCell rows
      |
      v
  derivation  ->  CellMetric rows (value, unit, confidence, sources, method)
      |
      v
  scoring  ->  PriorityScore + ScoreComponent rows, percentile ranks
      |
      v
  API  ->  map, evidence panel, analyst tools
```

Every stage records what it could not do. `DataGap` rows capture missing metrics,
unavailable sources and derivation notes; `IngestRun` records each run;
`DatasetVersion.message` carries a readable reason for any non-current status.

---

## The status model

Seven states, chosen so that no two operationally different situations share one.

| Status | Meaning |
|---|---|
| `CURRENT` | Fresh relative to the source's own update cadence |
| `AGING` | Older than expected but usable |
| `STALE` | Old enough that conclusions drawn from it are suspect |
| `PARTIAL` | Succeeded, but the result set was capped or incomplete |
| `UNKNOWN` | Retrieved, but the source publishes no observation date |
| `FAILED` | Configured and attempted; the request errored |
| `UNAVAILABLE` | Configured but cannot be attempted — missing credential, or blocked |

`UNAVAILABLE` is an addition to the brief's list. A source missing an API key is
operationally different from one that failed, and both differ from one returning
stale data. Collapsing them would make the data health panel less useful than the
sum of its parts.

`UNKNOWN` is used for all OpenStreetMap sources, because OSM edit timestamps
record when someone touched the data, not when anyone surveyed the ground. Using
an edit date as a freshness signal would be a fabrication.

---

## Provenance model

Nothing is stored without its origin. Each `CellMetric` carries:

- `value`, `unit`
- `confidence` (0–1)
- `sources` — the dataset IDs that produced it
- `method` — a human-readable description of how it was computed
- `as_of_date` — `NULL` for time-invariant metrics

The unique constraint on `(cell_id, metric, as_of_date)` uses
`NULLS NOT DISTINCT`, because PostgreSQL otherwise treats every `NULL`
`as_of_date` as distinct and re-derivation silently duplicates every static
metric. This was a real bug; `repair_cell_metric_uniqueness()` in `core/db.py`
deduplicates and rebuilds the constraint on `initdb`.

---

## Scaling to another municipality

The whole of it:

1. Write `backend/firewatch/municipalities/<id>.yaml`.
2. Set `boundary.filter_value` to the name in the provincial boundary dataset.
3. Set `analysis.metric_crs` to the correct UTM zone, or omit it and let
   `utm_crs_for` choose.
4. Optionally add municipal ArcGIS sources.
5. `python -m firewatch run -m <id>`.

Outside British Columbia, the boundary adapter needs replacing — `bcgw_wfs` is
BC-specific. Everything else (CWFIS, ECCC, OSM, Terrarium, FIRMS) is national or
global and needs no change.

Enforced by tests rather than asserted:

- `test_no_municipality_name_is_hard_coded_in_the_engine` parses the AST of every
  module outside `sources/municipal/` and fails on a place name in any
  non-docstring string literal.
- `test_adding_a_municipality_needs_only_a_yaml_file` checks that every
  configured adapter is one of the portable set, or the optional municipal
  ArcGIS one.
- `test_every_adapter_named_in_config_is_registered` catches a typo at test time
  instead of halfway through a 15-minute ingest.

---

## Performance notes

First full West Vancouver run is about 15 minutes, dominated by the CWFIS hotspot
archive (18.5M rows upstream, queried by bounding box on the spatially indexed
geometry column, not by lat/lon between-filters, which timed out) and by the
Overpass building query.

Derivation is about 6 minutes for 15,798 cells at resolution 10, dominated by
sightlines. Scoring is under a minute.

GIST indexes exist on `features.geometry::geography` and
`analysis_cells.geometry::geography` because the derivation queries are
distance-based; without them the nearest-feature queries are unusably slow.
