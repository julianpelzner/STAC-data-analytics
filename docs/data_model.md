# Data Model: Synthetic Demand, Real Supply

This document defines the synthetic business dataset (customers, AOIs, orders), how it is generated, and how it joins to the real Sentinel-2 STAC metadata. It is the contract between the generator (`ingestion/generate_synthetic.py`), the BigQuery `raw` dataset, and the dbt models. Change it here first, then in code.

---

## 1. Core idea

The synthetic data models **demand**: who the customers are, where they want imagery, and when. The real STAC data is **supply**: what Sentinel-2 actually captured, and under what cloud conditions. Order outcomes come from joining the two in dbt: whether the order was fulfilled, when, and by which scene.

The generator never decides whether an order succeeds. Collection success and the cloud/revisit part of fulfillment time come from real satellite conditions.

```
customers ──< aois ──< orders ──(AOI geometry ∩ scene footprint, time window, cloud threshold)──> stac_items
 synthetic    synthetic  synthetic                                                                  real
                                                                                                     │
                                                                          int_order_fulfillment (dbt) ▼
                                                                fulfilled? which scene? fulfillment_hours
```

| Business question | Driven by | Synthetic assumptions involved |
|---|---|---|
| Q1: Collection success rate by region/conditions | Real cloud cover and revisit | Order windows and cloud thresholds per segment |
| Q2: Revenue per AOI by customer segment | Pricing rules | **Mostly synthetic**: price formula, discounts, rush share |
| Q3: AOI complexity vs. fulfillment time | Real acquisition timing + synthetic processing time | Processing-time function of complexity |

> **Honesty note for the case studies:** Q2 findings reflect the pricing assumptions in §5 and must be presented as a modelled scenario. Q1 is driven by real data. Q3 is a mix, and §6.3 explains which parts are which.

---

## 2. Entity-relationship diagram

```
┌──────────────┐ 1     n ┌──────────────┐ 1     n ┌──────────────┐ n     0..1 ┌──────────────┐
│  customers   │────────<│     aois     │────────<│    orders    │- - - - - - >│  stac_items  │
│ customer_id  │         │ aoi_id       │         │ order_id     │  (derived   │ item_id      │
│ segment      │         │ customer_id  │         │ aoi_id       │   in dbt)   │ region       │
│ contract_tier│         │ region ──────┼─────────┼──────────────┼────────────>│ acquired_at  │
└──────────────┘         │ geometry_wkt │         │ window_*     │             │ cloud_cover  │
                         └──────────────┘         │ max_cloud_.. │             │ geometry     │
                                                  └──────────────┘             └──────────────┘
```

- A customer has 1+ AOIs. An AOI has 1+ orders.
- `orders.customer_id` is redundant with `aois.customer_id`. It is kept to simplify joins, and a dbt test enforces that they match.
- `aois.region` must be one of the six regions in `ingestion/aois.yaml`. It is the coarse join key to `stac_items.region`.

---

## 3. Table schemas

All tables land in BigQuery `raw` 1:1 with the generated CSVs. Timestamps are UTC, and at **microsecond** precision — BigQuery `TIMESTAMP` is microsecond and rejects the nanosecond timestamps pandas writes to Parquet by default, so `transform.py` casts the STAC `acquired_at`/`ingested_at` to `datetime64[us]` before writing.

### 3.1 `customers`

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | PK, `CUST-00001` |
| `company_name` | STRING | Faker company name |
| `segment` | STRING | `agriculture`, `insurance`, `energy_utilities`, `government`, `research_ngo` |
| `contract_tier` | STRING | `self_serve`, `professional`, `enterprise` |
| `country` | STRING | ISO 3166-1 alpha-2 HQ country |
| `signup_date` | DATE | Before the customer's first order |

### 3.2 `aois`

| Column | Type | Description |
|---|---|---|
| `aoi_id` | STRING | PK, `AOI-00001` |
| `customer_id` | STRING | FK → `customers` |
| `aoi_name` | STRING | Human-readable label, e.g. `Field 12 - Story County` |
| `region` | STRING | One of the six STAC regions |
| `shape_type` | STRING | `field`, `floodplain`, `corridor`, `site`, `admin_block`, `study_area` |
| `geometry_wkt` | STRING | WGS84 polygon, fully inside the region bbox |
| `area_km2` | FLOAT | Geodesic area (computed, not sampled) |
| `vertex_count` | INTEGER | Exterior ring vertices, closing vertex excluded |
| `compactness` | FLOAT | Polsby-Popper: `4π·area / perimeter²` (1 = circle, → 0 = thin or jagged) |
| `created_at` | TIMESTAMP | On or after customer `signup_date` |

### 3.3 `orders`

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | PK, `ORD-000001` |
| `customer_id` | STRING | FK → `customers` (must equal the AOI's owner) |
| `aoi_id` | STRING | FK → `aois` |
| `ordered_at` | TIMESTAMP | When the order was placed |
| `window_start` | TIMESTAMP | Earliest acceptable acquisition (≥ `ordered_at`) |
| `window_end` | TIMESTAMP | Latest acceptable acquisition |
| `max_cloud_cover` | FLOAT | Acceptance threshold, 0–100 (scene-level `eo:cloud_cover`) |
| `priority` | STRING | `standard`, `rush` |
| `price_usd` | FLOAT | Quoted price (§5) |
| `processing_hours` | FLOAT | Synthetic post-acquisition processing effort, used only if fulfilled (§6.3) |

Orders have no status column. Fulfillment is **derived** in dbt and never generated.

---

## 4. Segment profiles

The generator reads these from `ingestion/synthetic/config.yaml`. The values below are the starting design. Tune them in config, not in code.

| Segment | Customer share | Shape types | Area (km²) | Vertices | Regions (weighted) | Cloud max | Window | Rush share | Tier mix |
|---|---|---|---|---|---|---|---|---|---|
| `agriculture` | 35% | `field` | 0.5–20 | 8–40 | us_midwest, western_europe, southeast_asia, sahel | ≤ 20 | 7–14 d | 5% | mostly self_serve / professional |
| `insurance` | 15% | `floodplain` | 10–200 | 12–30 | southeast_asia, sahel, us_midwest | ≤ 40 | 5–14 d | 45% | professional / enterprise |
| `energy_utilities` | 15% | `corridor` (80%), `site` (20%) | 5–150 | 6–60 | middle_east, us_midwest, amazon | ≤ 30 | ~30 d | 10% | mostly enterprise |
| `government` | 10% | `admin_block` | 500–2,500 | 4–8 | all six, extra weight western_europe, middle_east | ≤ 10 | 30–90 d | 2% | enterprise |
| `research_ngo` | 25% | `study_area` | 20–500 | 6–25 | amazon, sahel, southeast_asia | ≤ 50 | 30–60 d | 0% | self_serve |

What each segment represents:

- **Agriculture:** agronomy platforms and large farm operators monitoring crop health (NDVI) through the growing season. Many small, irregular fields. Strict cloud limits because clouds corrupt vegetation indices. Orders cluster in each region's growing season.
- **Insurance:** crop and catastrophe insurers verifying claims after floods, droughts and storms. Event-driven, bursty around wet/storm seasons. They trade cloud quality for speed, so rush orders are common.
- **Energy & utilities:** pipeline, transmission and solar/oil & gas operators monitoring infrastructure. Long, thin corridors with the lowest compactness. Steady monthly or quarterly schedules.
- **Government:** mapping agencies and environment ministries building cloud-free basemaps over large administrative blocks. Few, long-window orders with very strict cloud limits.
- **Research & NGO:** universities and conservation groups tracking deforestation and desertification. Lenient cloud limits and flexible windows, but heavily discounted.

Area caps reflect the region bboxes (~1°×1°, roughly 7,000–12,000 km² depending on latitude). Every AOI must fit fully inside its region's bbox.

### Volumes and seasonality

- Default scale: **~150 customers, ~500 AOIs, ~5,000 orders**. It is configurable.
- AOIs per customer: Poisson-ish, segment-weighted (agriculture and research have the most).
- Orders per AOI follow the segment cadence: agriculture and insurance are seasonal (weighted to the region's growing or wet season), energy is monthly or quarterly, government and research are sparse.
- **Date range:** all `window_start`/`window_end` values fall inside the STAC pull window. The generator takes `--start`/`--end`, with defaults matching `pull_stac_metadata` (trailing 12 full months). An order whose window extends past the available STAC data could never be fulfilled, which would bias Q1.
- **Determinism:** one `--seed` drives Faker, numpy and shapely sampling. The same seed and config produce byte-identical CSVs.

---

## 5. Pricing (drives Q2)

```
price_usd = max(min_order_fee,
                area_km2 × base_rate_per_km2
                         × tier_multiplier[contract_tier]
                         × priority_multiplier[priority]
                         × (1 − segment_discount[segment]))
```

| Parameter | Starting value |
|---|---|
| `base_rate_per_km2` | 8.00 USD |
| `min_order_fee` | 150 USD |
| `tier_multiplier` | self_serve 1.00, professional 0.90, enterprise 0.75 |
| `priority_multiplier` | standard 1.0, rush 1.5 |
| `segment_discount` | research_ngo 0.40, government 0.15 (volume), others 0.00 |

Small agricultural fields mostly hit the minimum fee, so agriculture has a high price per km² but low price per order. Government has the lowest price per km² but the highest price per order. All values are illustrative and live in config.

**Revenue definitions** (enforced in the marts):
- **Booked revenue:** sum of `price_usd` for all orders.
- **Delivered revenue:** sum of `price_usd` for fulfilled orders only. **Q2 uses delivered revenue**, so collection failures (real data) feed into revenue per AOI.

---

## 6. Join logic (dbt `int_order_fulfillment`)

### 6.1 Candidate scenes

A STAC scene is a **candidate** for an order when all of these are true:

1. `stac_items.region = aois.region` (coarse pre-filter, cheap)
2. `ST_INTERSECTS(scene_geometry, aoi_geometry)`
3. `window_start ≤ acquired_at ≤ window_end`
4. `cloud_cover ≤ max_cloud_cover`

### 6.2 Fulfillment

An order is **fulfilled** by the earliest acquisition date on which its candidate scenes cover **≥ 95% of the AOI area**. Scenes from the same date are combined with `ST_UNION_AGG`, because adjacent tiles from the same satellite pass can jointly cover an AOI that straddles a tile boundary.

```
coverage(order, date) = ST_AREA(ST_INTERSECTION(ST_UNION_AGG(scene_geoms), aoi_geom)) / ST_AREA(aoi_geom)
fulfilled_on          = MIN(date) WHERE coverage ≥ 0.95
```

Output grain: one row per `order_id`, with:

| Column | Description |
|---|---|
| `is_fulfilled` | Whether any date reached coverage ≥ 95% inside the window |
| `fulfilling_item_ids` | Scene(s) used on that date |
| `acquired_at` | Earliest acquisition timestamp among those scenes |
| `scene_cloud_cover` | Max cloud cover among those scenes |
| `candidate_scene_count` | Scenes that passed 6.1, including those that did not complete coverage (useful for Q1 diagnostics) |
| `rejected_for_cloud_count` | Scenes that would have qualified except for `cloud_cover` (attributes failures to cloud vs. no pass) |
| `delivered_at` | `acquired_at + processing_hours` |
| `fulfillment_hours` | `delivered_at − ordered_at` |

### 6.3 Fulfillment time: real vs. synthetic parts

`fulfillment_hours = (acquired_at − ordered_at) + processing_hours`

- **`acquired_at − ordered_at`** is **real**: it depends on Sentinel-2 revisit and actual cloud cover.
- **`processing_hours`** is **synthetic**: mosaicking, orthorectification and QA effort, which grows with AOI complexity. The generator draws it per order:

```
processing_hours = base_hours[priority]
                 × (1 + α · log1p(area_km2))
                 × (1 + β · (1 − compactness))
                 × (1 + γ · log1p(vertex_count))
                 × lognormal_noise(σ)
```

Starting values: `base_hours` standard 24 / rush 8, `α = 0.10`, `β = 0.50`, `γ = 0.15`, `σ = 0.25`.

The case study for Q3 should report both parts separately. An effect in the real acquisition delay (for example, large AOIs needing multi-tile coverage on a single date) is a finding. An effect in processing time is a modelled assumption.

### 6.4 Known limitations

- **Cloud cover is scene-level**, not AOI-level. A 30% cloudy tile may be clear over a small AOI, or the reverse. This is acceptable for v1. A v2 could estimate AOI-level cloud from the SCL band, which requires pixel access.
- **Sentinel-2 is not tasked.** Real satellites capture on a fixed revisit (~5 days), so "orders" here model buying from an archive or subscription feed, not commanding a satellite. Fulfillment is about waiting for a usable pass, which is realistic for optical archive vendors.
- **STAC coverage is limited to the six ~1° regions.** Every AOI is generated inside one of them by construction.

---

## 7. Data quality rules

Checked by pytest on the generator output and re-asserted as dbt tests on `raw`/staging:

| Rule | pytest | dbt |
|---|---|---|
| PKs unique and not null | ✓ | `unique`, `not_null` |
| All FKs resolve; `orders.customer_id` = owning AOI's `customer_id` | ✓ | `relationships` + singular test |
| `segment`, `contract_tier`, `priority`, `shape_type`, `region` in allowed sets | ✓ | `accepted_values` |
| AOI geometry valid and fully inside its region bbox | ✓ | singular (`ST_COVERS`) |
| `area_km2 > 0`, `0 < compactness ≤ 1`, `vertex_count ≥ 3` | ✓ | singular |
| `signup_date ≤ aoi.created_at ≤ ordered_at ≤ window_start < window_end` | ✓ | singular |
| Order windows within the STAC pull range | ✓ | singular (against `stac_items` min/max) |
| `price_usd ≥ min_order_fee`, `processing_hours > 0` | ✓ | singular |
| `fulfillment_hours` never negative | — | singular (on `int_order_fulfillment`) |
| Same seed produces identical output | ✓ | — |

---

## 8. Files and outputs

| Path | Contents |
|---|---|
| `ingestion/generate_synthetic.py` | CLI: `python -m ingestion.generate_synthetic --seed 42 [--start --end --output-dir]` |
| `ingestion/synthetic/config.yaml` | Segment profiles, pricing, processing-time parameters, volumes |
| `ingestion/tests/test_generate_synthetic.py` | The pytest rules in §7 |
| `data/raw/synthetic/` | Full generated CSVs (gitignored) |
| `data/raw_sample/{customers,aois,orders}.csv` | Committed sample, so the project runs without credentials |

`ingestion/load_to_bigquery.py` writes these to `gs://<project>-raw/synthetic/<table>/ingest_date=YYYY-MM-DD/` and loads them into `raw.customers`, `raw.aois` and `raw.orders` (and the STAC parquet into `raw.stac_items`), 1:1 with `WRITE_TRUNCATE`. It runs the §7 PK/column checks locally before upload and blocks the whole batch on any failure.

---

## Change log

| Date | Change |
|---|---|
| 2026-10-31 | Initial design: segments, schemas, pricing, fulfillment join |
| 2026-11-03 | Built the GCS→BigQuery loader (`ingestion/load_to_bigquery.py`); added `raw.aois` to dbt sources |
