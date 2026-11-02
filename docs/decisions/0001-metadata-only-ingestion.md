# ADR 0001: Pull STAC metadata only, not imagery

- **Status:** Accepted
- **Date:** 2026-11-01
- **Deciders:** Project owner (solo)
- **Relates to:** Q1 (collection success by region/conditions), Q3 (AOI complexity vs. fulfillment time), `ingestion/`, [data_model.md](../data_model.md)

---

## Context

The real "supply" side of this project comes from Sentinel-2 scenes served
through a STAC API (Planetary Computer by default,
`ingestion/config.py:DEFAULT_STAC_API_URL`). A STAC item has two separable
parts: the **metadata** (geometry/footprint, acquisition time, cloud cover,
orbit, collection) and the **assets** — the actual raster bands, which are
large files fetched from blob storage.

The three business questions are answered entirely from the metadata:

- **Q1** needs cloud cover and acquisition timing per region → `cloud_cover`,
  `acquired_at`, `region`.
- **Q3** needs when a usable scene was acquired relative to an order window →
  `acquired_at` + footprint geometry.
- Q2 is driven by synthetic pricing and doesn't touch the rasters at all.

Nothing downstream — the dbt join (`data_model.md`), the marts, or the
dashboards — reads pixels. The join is AOI geometry ∩ scene footprint within a
time window under a cloud threshold, all of which is metadata.

Constraints at the time: a free-tier GCP budget, a repo meant to run on a
laptop and in CI without heavy infra, and a scope deliberately kept to an
analytics-stack demonstration rather than an image-processing pipeline.

## Decision

Ingestion pulls **metadata only**. `ingestion/transform.py:COLUMNS` is the
canonical output schema (item id, collection, region, acquisition time, cloud
and orbit properties, bbox, centroid, geometry) — it carries **no asset hrefs
and no raster data**. Because only metadata is read, no Planetary Computer SAS
asset signing is performed (`ingestion/stac_client.py`).

## Alternatives considered

| Option | Why not |
|---|---|
| Download the raster assets (COGs) too | Hundreds of MB–GB per AOI/time window; blows the free-tier budget and storage, needs SAS signing and retry/throughput handling, and adds a GIS-processing concern none of the three questions require. |
| Download a few rasters for thumbnails/visuals | Still pulls in SAS signing and asset-byte handling for a purely cosmetic gain; the geospatial notebook can render footprints and cloud stats from metadata alone. |
| Clip/compute on pixels at ingest (e.g. real cloud masks) | Duplicates work the STAC `eo:cloud_cover` property already provides at the scene level, which is the grain the business questions operate at. |

## Consequences

- **Positive:** Ingestion stays cheap, fast, laptop- and CI-friendly, and needs
  no asset authentication. Pulls are small enough to commit a stratified sample
  (`data/raw_sample/`) so the project runs without credentials. Scope stays on
  the analytics stack, not image processing.
- **Negative / trade-off:** The project can't do any true pixel-level analysis
  (actual cloud masking, NDVI, visual chips). Collection "success" is modelled
  from scene-level cloud cover and revisit, not from inspecting imagery — a
  limitation the case studies must state honestly.
- **Follow-ups:** If a future question needs pixels, that's a *new* decision
  (new ADR): it reintroduces SAS signing, asset storage, and cost guardrails,
  and should be scoped as a distinct ingestion path rather than bolted onto the
  metadata pull.

## Notes

- Scene-level cloud cover comes from the STAC `eo:cloud_cover` property; see the
  `PROPERTY_COLUMNS` mapping in `ingestion/transform.py`.
- The metadata-only rationale is also noted inline at
  `ingestion/stac_client.py` (the `open_client` docstring).
