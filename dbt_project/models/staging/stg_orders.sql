-- Orders (demand). Note there is no status column by design: whether an order
-- was fulfilled, when, and by which scene is derived in int_order_fulfillment
-- by joining to real STAC supply, never generated (data_model.md §6).
with source as (
    select * from {{ source('raw', 'orders') }}
)

select
    order_id,
    customer_id,
    aoi_id,
    ordered_at,
    window_start,
    window_end,
    max_cloud_cover,
    priority,
    price_usd,
    processing_hours
from source
