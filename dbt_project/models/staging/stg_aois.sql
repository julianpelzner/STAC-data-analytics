-- Areas of interest, one per order target. The WKT string is parsed to a
-- WGS84 GEOGRAPHY here, once, so every downstream model reads the typed column
-- instead of re-parsing. `make_valid => true` guards the ring-orientation trap
-- where a mis-wound polygon is read as its planet-sized complement (ADR-0002).
with source as (
    select * from {{ source('raw', 'aois') }}
)

select
    aoi_id,
    customer_id,
    aoi_name,
    region,
    shape_type,
    area_km2,
    vertex_count,
    compactness,
    created_at,
    st_geogfromtext(geometry_wkt, make_valid => true) as aoi_geog
from source
