-- Customers (demand side), 1:1 with raw.customers. Light staging only:
-- a stable column list so downstream models don't depend on raw column order.
with source as (
    select * from {{ source('raw', 'customers') }}
)

select
    customer_id,
    company_name,
    segment,
    contract_tier,
    country,
    signup_date
from source
