# dbt_project

dbt-bigquery project that turns the raw STAC imagery and synthetic business tables into one mart per business question (see the [root README](../README.md#business-questions)).

> **Status:** scaffold only. The project config, profile and folder layout are in place; sources and models arrive with the staging/intermediate/marts work on the roadmap.

## Setup

```bash
# from the repo root
pip install -r requirements-dbt.txt          # dbt-core + dbt-bigquery 1.12.x
gcloud auth application-default login        # local auth (Application Default Credentials)

cd dbt_project
dbt deps                                     # installs dbt_utils (pinned in package-lock.yml)
dbt debug                                    # checks config + BigQuery connection
```

Always run dbt from inside `dbt_project/`. dbt picks up the committed `profiles.yml` from the working directory, and it reads the repo-root `.env` automatically, so you don't need to `source` it first.

## Configuration

`profiles.yml` holds no secrets. Every value comes from an environment variable (see [`.env.example`](../.env.example)):

| Variable | Required | Default | Used for |
|---|---|---|---|
| `GCP_PROJECT_ID` | yes | — | BigQuery project (dbt fails fast if it's missing) |
| `BIGQUERY_DATASET` | no | `satellite_analytics` | Base dataset for the `prod` target |
| `DBT_DEV_DATASET` | no | `dbt_dev` | Base dataset for the `dev` target |
| `BIGQUERY_LOCATION` | no | `US` | Dataset location |
| `DBT_TARGET` | no | `dev` | Which target to run against |

Both targets use `method: oauth`, which resolves Application Default Credentials. Locally that means `gcloud auth application-default login`. In CI, set `GOOGLE_APPLICATION_CREDENTIALS` to point at a service-account key. Every query is capped at 1 GB scanned (`maximum_bytes_billed`) so a bad model can't eat into the BigQuery free tier.

## Layout

```
dbt_project/
├── models/
│   ├── staging/        # 1:1 with raw sources: rename, cast, clean → views
│   ├── intermediate/   # joins and business logic (orders ↔ imagery) → views
│   └── marts/          # one table per business question → tables
├── tests/              # singular SQL tests for business-logic assumptions
├── macros/
├── seeds/
├── snapshots/
└── analyses/
```

Each layer builds into its own dataset, named `<target dataset>_<layer>`. For example, `dbt run` against dev writes to `dbt_dev_staging` and `dbt_dev_marts`, and prod writes to `satellite_analytics_marts`. Model and column descriptions are pushed into BigQuery (`persist_docs`), so the BI layer sees the same documentation.

## Conventions

- **Naming:** `stg_<source>__<entity>` → `int_<entity>_<verb>` → mart named after the question it answers (e.g. `collection_performance`).
- **Tests:** generic tests (`not_null`, `unique`, `relationships`, `accepted_values`, `dbt_utils.*`) go in each layer's YAML next to the models. Assumptions that span models go in `tests/` as singular SQL tests (e.g. `fulfillment_hours` is never negative).
- **Grain:** STAC items are one row per `(item_id, region)`, because a scene can fall inside more than one AOI. Test this with `dbt_utils.unique_combination_of_columns`, not with `unique` on `item_id`.
