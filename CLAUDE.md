# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Portfolio analytics stack: real Sentinel-2 STAC imagery metadata + synthetic business data → GCS/BigQuery → dbt (staging → intermediate → marts) → BI dashboards and a geospatial notebook. Everything is meant to trace back to three business questions (see README): collection success rate by region/conditions, revenue per AOI by customer segment, and AOI complexity vs. fulfillment time.

**Built so far:** STAC ingestion, the synthetic business-data generator, and the GCS→BigQuery loader (all in `ingestion/`); a scaffolded dbt project (`dbt_project/`: config, profiles and layer folders, but no models yet); and the GCP backend (bucket, `raw` + `satellite_analytics` datasets, service account; see `docs/gcp_setup.md`). **Not built yet:** dbt models, `.github/workflows/ci.yml`. `case_studies/`, `dashboards/` and `notebooks/` hold placeholder READMEs. Check the README roadmap before assuming something exists.


## Commands

A local virtualenv lives at `.venv/` (Python 3.12).

```bash
pip install -r requirements-dev.txt

# Tests (no network; pyproject sets pythonpath="." and testpaths=ingestion/tests)
python -m pytest
python -m pytest ingestion/tests/test_validate.py::test_duplicates_fail

# STAC pull — must be run as a module from the repo root
python -m ingestion.pull_stac_metadata                       # all AOIs, trailing 12 full months
python -m ingestion.pull_stac_metadata --regions sahel amazon --start 2026-08-01 --end 2026-09-30
python -m ingestion.pull_stac_metadata --max-items-per-window 5   # quick live smoke test

# Synthetic business data (deterministic; see docs/data_model.md)
python -m ingestion.generate_synthetic --seed 42

# Load local raw files -> GCS -> BigQuery raw.* (needs ADC + GCP_PROJECT_ID/GCS_BUCKET_NAME)
python -m ingestion.load_to_bigquery --dry-run      # DQ checks only, touches no GCP
python -m ingestion.load_to_bigquery --only customers orders

# dbt
cd dbt_project && dbt deps && dbt parse    # validate the project
dbt build --target dev                      # builds into dbt_dev_<layer>

```

Linting/formatting: `ruff check .` and `ruff format .` for Python (config in `pyproject.toml`). SQL uses `sqlfluff` (`.sqlfluff`, BigQuery dialect, dbt templater) once dbt models exist. `pre-commit install` activates both on commit (`.pre-commit-config.yaml`).

## Ingestion architecture

Pipeline in `ingestion/pull_stac_metadata.py:main`: load AOIs → search → flatten → validate → write.

- `config.py` — loads `.env` from repo root at import time; defines all output paths relative to `REPO_ROOT`. AOIs come from `ingestion/aois.yaml` (~1°×1° bboxes chosen to contrast climates/cloud regimes; bbox order is `[min_lon, min_lat, max_lon, max_lat]`).
- `stac_client.py` — searches are split into calendar-month windows per AOI (bounds result size per request); urllib3 `Retry` handles 429/5xx including POST. Metadata only, so no Planetary Computer SAS signing.
- `transform.py` — `PROPERTY_COLUMNS` maps output column → STAC property key; `COLUMNS` is the canonical output schema, with explicit dtype lists (`FLOAT_COLUMNS`, `STRING_COLUMNS`, `relative_orbit` as `Int64`). Adding a column means updating `COLUMNS` and the right dtype list. Grain is one row per `(item_id, region)` — the same scene can legitimately appear under multiple AOIs.
- `validate.py` — returns a `ValidationReport`; `errors` block writing, `warnings` only log. Validation checks that every column in `transform.COLUMNS` is present, so schema changes flow through automatically.
- CLI behavior: on validation errors nothing is written and exit code is 1 (unless `--no-fail-on-validation`); unknown `--regions` → exit 2. A JSON validation report is written on **every** run, including failures.

Outputs: full pulls and validation reports go to `data/raw/` (gitignored); a stratified ~500-row sample is overwritten at `data/raw_sample/stac_items_sample.parquet`, which **is committed** so the project runs without credentials. `.gitignore` excludes all parquet/csv under `data/` except `data/raw_sample/`.

## Loader (`ingestion/load_to_bigquery.py`)

Lands the local raw files into GCS and BigQuery `raw.*`. One `Source` spec per table drives everything: local path, GCS prefix, format, PK, and an explicit BigQuery schema. Every table loads with its schema and **never** autodetect — BigQuery can't store the nanosecond timestamps pandas writes to Parquet by default, and under autodetect `acquired_at`/`ingested_at` land silently as `INT64`; an explicit schema turns that into a loud load failure instead. The synthetic schemas (`CUSTOMERS_SCHEMA` etc.) are hand-written; `STAC_SCHEMA` is derived from `transform.COLUMNS` + its dtype lists, so a new STAC column flows through automatically (keep `transform.COLUMNS` and `docs/data_model.md` §3 in sync). Flow per run: read each file → `check_dataframe` DQ (non-empty, all columns present, PK unique + non-null) → upload to `gs://<bucket>/<prefix>/ingest_date=YYYY-MM-DD/<file>` (immutable, replayable) → `load_table_from_uri` with `WRITE_TRUNCATE` → post-load row-count check. DQ runs on **all** sources before anything uploads, so one bad file blocks the whole batch (exit 1), matching `pull_stac_metadata`'s validation gate; `--no-fail-on-dq` loads only the sources that pass. `main(argv, storage_client=, bq_client=)` injects both GCP clients, so tests use the `fake_storage_cls`/`fake_bq_cls` fixtures and never hit the network.

## Testing conventions

- Tests never hit the network. `main(argv, client=...)` accepts an injected client; `conftest.py` provides `FakeClient` (filters items by the requested datetime window and records `calls`) and a `make_item(item_id=, dt=, properties=, drop=)` factory built from `tests/fixtures/sentinel2_l2a_item.json`.
- CLI tests pass every path flag (`--aoi-config`, `--output-dir`, `--sample-path`, `--report-dir`) pointing at `tmp_path` so they never touch `data/`. Keep that pattern for new CLI tests.

## Environment

`.env` lives at the repo root (template: `.env.example`). Ingestion uses only `STAC_API_URL` (defaults to Planetary Computer). dbt reads `GCP_PROJECT_ID` (required), `BIGQUERY_DATASET`, `BIGQUERY_RAW_DATASET`, `DBT_DEV_DATASET`, `BIGQUERY_LOCATION` and `DBT_TARGET`; see `dbt_project/README.md`. `GCS_BUCKET_NAME` is reserved for the upcoming loaders. Run dbt from inside `dbt_project/`. Auth is via ADC (`gcloud auth application-default login`), never a key file.

