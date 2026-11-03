-- Sentinel-2 scene metadata (supply side), one row per (item_id, region) —
-- the same scene can legitimately appear under more than one AOI region.
-- The GeoJSON footprint is parsed to GEOGRAPHY here (make_valid => true, see
-- ADR-0002); the cloud/quality percentages are kept for Q1 condition analysis.
with source as (
    select * from {{ source('raw', 'stac_items') }}
)

select
    item_id,
    collection,
    region,
    acquired_at,
    acquired_date,
    platform,
    constellation,
    mgrs_tile,
    cloud_cover,
    nodata_pct,
    high_proba_clouds_pct,
    medium_proba_clouds_pct,
    thin_cirrus_pct,
    cloud_shadow_pct,
    vegetation_pct,
    water_pct,
    snow_ice_pct,
    solar_zenith,
    solar_azimuth,
    relative_orbit,
    orbit_state,
    processing_baseline,
    centroid_lon,
    centroid_lat,
    stac_api_url,
    ingested_at,
    st_geogfromgeojson(geometry_geojson, make_valid => true) as scene_geog
from source
