# Fire Watch

**Yellow Duck Labs — a municipal wildfire operating picture, built from public data.**

Yellow Duck Labs exists as a silent sentry: to safeguard the continuity of
communities, cultures, ecosystems, and the ways of life entrusted to our care.
Fire Watch is the first step — protecting communities against the growing reach
of wildfire, starting with the District Municipality of West Vancouver.

This first iteration does not detect or suppress anything. It answers four
questions honestly, for every 100-metre cell of a municipality, and it is
explicit about where the answers run out:

| Question | Where it is answered |
|---|---|
| What are we preserving here? | Location panel, "What we preserve here" |
| What wildfire threat exists here, now and historically? | "What threat exists here", plus the timeline |
| What protections, access, water, observation and response assets exist? | "What already protects here" |
| Where is the remaining gap? | "Who is watching here" and "What nobody knows" |

The fourth question is the one that matters most, and it is the one this build
answers with real analysis rather than a proxy. See
[The observation gap](#the-observation-gap-the-central-finding).

---

## Quick start

You need Docker and about 15 minutes for the first ingest.

```bash
cp .env.example .env          # optional keys: FIRMS_MAP_KEY, ANTHROPIC_API_KEY
docker compose up -d db
docker compose run --rm api python -m firewatch initdb
docker compose run --rm api python -m firewatch run -m west-vancouver
docker compose up -d api web
```

Open <http://localhost:3000>.

No API keys are required. Every default source is keyless and openly licensed.
Two optional keys add capability and are reported as `UNAVAILABLE` when absent
rather than silently skipped:

| Key | Adds | Without it |
|---|---|---|
| `FIRMS_MAP_KEY` | NASA FIRMS near-real-time detections | CWFIS hotspots still provide detections |
| `ANTHROPIC_API_KEY` | Natural-language analyst | Analyst runs the same tools and prints their output verbatim |

### Commands

```bash
python -m firewatch sources                        # adapters and configured municipalities
python -m firewatch run      -m west-vancouver     # ingest, derive, score
python -m firewatch status   -m west-vancouver     # data health and metric coverage
python -m firewatch backtest -m west-vancouver --date 2023-08-15
python -m pytest                                   # 166 tests
```

---

## What is actually in it

Two municipalities are configured and ingested:

| | West Vancouver | Kelowna |
|---|---|---|
| Purpose | The mission | Proof of portability |
| H3 resolution | 10 (~0.015 km² cells) | 9 (~0.1 km² cells) |
| Cells | 15,798 (8,019 in boundary) | 4,982 |
| UTM zone | 10N | 11N |
| Municipal-tier sources | 4 configured | none, deliberately |
| Historical fire incidents | 127 | 1,056 |
| Buildings | 10,152 (OSM; municipal blocked) | 45,123 (OSM only) |

Kelowna was onboarded with **no code changes** — one YAML file. It is
deliberately a hard second case: a different UTM zone, no municipal data tier at
all, interior dry-belt fuels instead of coastal rainforest, and a severe fire
history where West Vancouver's records are nearly empty. A test
(`test_no_municipality_name_is_hard_coded_in_the_engine`) enforces the claim by
parsing the AST of every engine module and failing on any place name in a live
string literal.

### Data sources

Sixteen to twenty sources per municipality, each carrying licence, attribution,
version, observation time and status.

| Tier | Sources |
|---|---|
| Municipal | West Vancouver ArcGIS: buildings, roads, water utilities, parks |
| Provincial | BCGW: legal boundary, historical fire perimeters, historical incidents |
| Federal | CWFIS fire weather stations, hotspot archive, fire danger, WMS overlays; ECCC GeoMet stations and hourly observations |
| Remote sensing | NASA FIRMS; Tilezen Joerd terrain tiles |
| Community | OpenStreetMap: buildings, roads, water assets, parks, fire stations, vegetation |

Conflicts resolve by precedence: municipal > provincial > federal > remote
sensing > community > model-derived. When a higher tier is unavailable, the
substitution is recorded and shown, never silent.

---

## The observation gap: the central finding

The brief asks where a fire could grow unseen. The obvious answer — distance to
the nearest road — is close to worthless in steep coastal terrain. A gully 80 m
below a highway can be completely invisible from it, while a ridge 3 km away is
in plain view from half the municipality.

So visibility is computed properly, as a line-of-sight test against the DEM:

- 19,471 observer points sampled at even ground spacing along the road network
  (resampled, so a winding road is not credited for bending);
- for each cell, a ray test from every observer within range to a **10 m smoke
  column**, not to the ground surface, because the column is what a person
  notices first;
- observers weighted by distance, since a clear view from 300 m is worth far
  more for early detection than one from 4 km;
- earth curvature included, which is what puts a horizon in the model at all.

This produces real spatial variation — a large concealed zone in the northeast
backcountry and scattered pockets along the upper slopes — where the previous
proxy produced a nearly uniform field.

**What it does not say.** Intervening canopy is a typical height for the CWFIS
FBP class, not measured LiDAR, so real visibility under dense timber is *no
better* than this figure. It says nothing about whether anyone is looking, what
the weather is doing, or whether smoke would be recognised and reported. Those
limits ship with the metric, in the evidence panel.

Dedicated observation — cameras, patrols, lookouts, drones — is reported as
entirely absent, because no such data exists for any part of either
municipality. It is held at a low weight so it cannot flatten the ranking. That
section is precisely what a Yellow Duck sensor network would populate.

---

## The priority score

Version 0.1. Deterministic; no language model touches any number.

The brief specifies a five-way product. Taken literally that collapses toward
zero and stops being readable, so the implementation uses the **geometric mean**
— the same product raised to 1/5. It preserves the ordering and the important
"one low component suppresses the whole score" behaviour on a legible 0–1 scale.
This deviation is disclosed in the payload itself, in `formula_note`.

Five components, each with weighted signals, each signal carrying the metrics it
used, the metrics it lacked, and a plain-language rationale:

| Component | Driven by |
|---|---|
| Ignition likelihood | hotspot history, fine fuel moisture, proximity to access, FBP fuel / vegetation presence |
| Spread potential | slope, aspect, CWFIS FBP fuel type, vegetation continuity, ISI, wind |
| Consequence / exposure | structure count and proximity, community land. **Property value is deliberately not used.** |
| Observation gap | line-of-sight through terrain and typical FBP canopy (0.55), distance to nearest clear vantage (0.20), dedicated observation (0.15), detection recency (0.10) |
| Access difficulty **proxy** | road distance, ruggedness, slope, water asset distance. **This is not response time.** |

Hazard, exposure, current conditions and operational gap are also reported
separately, because collapsing them into one number is the failure mode the
brief warns against.

Two readings are available on the map. Absolute priority is comparable across
municipalities and dates. Percentile rank is comparable only within one
municipality on one date, and its thresholds are computed over cells inside the
legal boundary so that buffer cells cannot skew the distribution.

### It is a hypothesis, not a truth

The score has not been validated against outcomes. `backtest` exists to find out
where it is wrong, and reports rather than optimises — tuning against West
Vancouver's handful of incidents would produce an overfit that fails everywhere
else.

---

## Honesty machinery

This is the part that took the most care, because a wildfire tool that guesses
confidently is worse than no tool.

**Every source reports a status**: `CURRENT`, `AGING`, `STALE`, `PARTIAL`,
`UNKNOWN`, `FAILED`, `UNAVAILABLE`. A source that could not be reached never
looks like a source that legitimately returned nothing.

West Vancouver's own GIS is behind Cloudflare and returns 403 to any automated
client. Rather than dropping those sources, they stay configured and report
`UNAVAILABLE` with a readable reason, and the panel says community data is
standing in. Kelowna, with no municipal tier configured at all, says *that*
instead of showing an empty gap list that would read as "nothing missing".

**Missing data is never a default.** An absent input yields `None`, not zero. A
cell that cannot produce at least three of five components gets no overall
score, and says which components were computed so they can still be read.

**Provenance reaches the UI.** Every metric carries its source datasets, method
description, observation time and confidence. Nothing renders as a bare number.

**The analyst is structurally grounded.** Every map fact must come from a tool
call; the tools attach provenance and explicit `unknown` markers. Dispatch time,
crew availability, hydrant flow, apparatus access and official fire status are
named individually as things it must never state. With no API key it performs no
synthesis at all — the same tools, output verbatim, and it says so.

---

## Bugs the tests found

The test suite was written to check properties against known geometry rather
than to lock in existing output. That distinction mattered — it found four real
bugs, three of which were silently producing plausible wrong answers:

**Aspect was inverted north-south.** The northing gradient was negated twice, so
aspect was correct east-west and backwards north-south: every south-facing slope
was reported as north-facing. Aspect feeds the dryness factor in spread
potential, so the effect was to credit the driest slopes to the wettest ones.
West Vancouver sits on the north shore of Burrard Inlet and its terrain falls
south toward the water; before the fix, 48% of cells read north-facing and 8%
south-facing. After, those numbers swap, which is what the geography says.

**Earth curvature had the wrong sign.** The correction was subtracted from
intervening terrain instead of added. Ground between two points bulges *above*
the chord joining them, so subtracting removed the horizon from the model
entirely. At municipal range the term is 0.18 m over 3 km and barely matters; the
sign error mattered because it made distant ground less obstructive than flat
geometry implies.

**A cell with no data ranked highest in the municipality.** Three observation-gap
signals correctly treat absence of evidence as evidence of a gap — "no road has a
clear view of here" is a finding. But they consumed no metric, so a cell with
nothing known about it satisfied all three, scored a maximal observation gap, and
came out at 1.0, "Very high". The component is now gated on proof that the
visibility computation actually ran, and "we looked and nothing can see this
place" is distinguished from "we never looked".

**The analyst leaked one municipality into another.** A suggested question asked
what "West Vancouver Fire & Rescue" needed to validate, and offered it to Kelowna
users too.

Two smaller ones: an empty geometric mean returned 0.0, a claim of minimal
priority, where it should return no claim; and a null station name crashed an
entire ingest.

### Test coverage

166 tests, no network access required.

| File | What it guards |
|---|---|
| `test_geo.py` | CRS, buffers, H3 grid, terrarium decoding, slope, aspect, line-of-sight against a flat plain, a blocking ridge and a gully below a road |
| `test_scoring.py` | ramp clamping, monotonicity per driver, geometric-mean suppression, and above all the honesty properties: missing inputs named, confidence falling, no invented values |
| `test_config.py` | every config loads, every adapter exists, every source declares licence and attribution, and no place name in engine code |
| `test_sources.py` | date and value parsing against real-world formats, bot-protection pages explained rather than quoted, failure distinguished from emptiness |
| `test_ai_grounding.py` | every forbidden claim named in the prompt, tool/schema parity, no municipality argument the model could redirect, deterministic mode synthesising nothing |

---

## Architecture

```
backend/firewatch/
  core/geo/        CRS, H3 grid, terrain, sightline
  core/scoring/    normalisers and the priority model
  core/ai/         analyst, tools, document retrieval
  core/derive.py   per-cell metric derivation
  sources/         one adapter per source family
  municipalities/  one YAML per municipality — the only place-specific files
  api/             FastAPI routers
web/               Next.js + MapLibre GL JS
```

Backend: Python 3.12, FastAPI, PostgreSQL 16 + PostGIS 3.4, SQLAlchemy.
Frontend: Next.js, MapLibre GL JS, Tailwind. Basemap: OpenFreeMap (keyless).

Deviations from the brief, and why, are in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The main ones: a single
`features` table with a `feature_kind` discriminator plus SQL views, rather than
a table per feature type; and the geometric mean discussed above.

---

## What is deliberately not built

Per the brief: no autonomous detection, no suppression, no dispatch integration,
no evacuation modelling, no property valuation, no real-time fire spread
simulation, no camera or sensor hardware.

## The largest known gaps

1. **West Vancouver's own GIS**, currently blocked by the municipal CDN.
   Municipal footprints, road classification and the hydrant network would
   upgrade the top precedence tier from unavailable to authoritative.
2. **Municipal LiDAR.** A global 30 m composite DEM is used for portability;
   West Vancouver publishes bare-earth LiDAR that would sharpen slope,
   ruggedness and every sight line. Measured canopy would replace FBP-typical
   stand heights in the visibility model.
3. **Fuel inventory, not fuel type.** The CWFIS 100 m FBP grid is now used for
   type and typical canopy. Stand age, crown closure, surface load and ladder
   fuels are still unknown. A local inventory or VRI would change the spread
   numbers.
4. **Everything operational.** Apparatus access on steep dead-end roads, hydrant
   flow and pressure, dispatch and travel times, gates and seasonal closures,
   crew availability, local drainage winds in the Capilano, Brothers, Nelson and
   Cypress creeks, FireSmart treatment extent — and responder judgement about
   which areas actually worry them. None of it is in any dataset reachable here,
   and all of it is declared in each municipality's `known_unknowns`.

## Licensing

Fire Watch aggregates openly licensed data and passes attribution through to the
UI: Open Government Licence – British Columbia (BCGW), Open Government Licence –
Canada (CWFIS, ECCC), ODbL 1.0 (© OpenStreetMap contributors), NASA Earth
Science Data policy (FIRMS), and the Tilezen Joerd attribution list for
elevation. Per-source licence and attribution are visible in the Data health
panel and returned by the provenance API.
