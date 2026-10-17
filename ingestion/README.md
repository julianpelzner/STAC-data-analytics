Python scripts that pull real STAC imagery metadata, generate synthetic business data, and load both into BigQuery raw tables.

## STAC metadata pull

Pulls Sentinel-2 L2A item **metadata** (no imagery) from the [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/) STAC API for the regional AOIs in [`aois.yaml`](aois.yaml). It then flattens the results to one row per `(item_id, region)`, runs data-quality checks, and writes Parquet.

```bash
pip install -r requirements-dev.txt

# Default: all AOIs, trailing 12 full months
python -m ingestion.pull_stac_metadata

# Subset / custom window
python -m ingestion.pull_stac_metadata --regions sahel amazon --start 2026-08-01 --end 2026-09-30
```

Outputs:
- `data/raw/stac_items/stac_items_<start>_<end>.parquet`: the full pull (gitignored)
- `data/raw_sample/stac_items_sample.parquet`: a stratified ~500-row sample, committed so the project runs without credentials
- `data/raw/validation_reports/validation_<start>_<end>_<run timestamp>.json`: one report per run with errors, warnings, row counts and null cloud-cover rate per region, and whether data was written (gitignored)

The search runs one month at a time per AOI, and HTTP 429/5xx responses are retried with backoff. If validation fails (nulls in key fields, duplicates, `cloud_cover` outside [0, 100], dates outside the window, invalid bboxes), nothing is written and the command exits non-zero. Pass `--no-fail-on-validation` to override that. A validation report is saved on every run, including failed ones, so you can review what went wrong. `STAC_API_URL` is read from `.env` and defaults to the Planetary Computer.

| Module | Role |
|---|---|
| `config.py` | env vars, paths, AOI loading |
| `stac_client.py` | client with retries, monthly-chunked search |
| `transform.py` | STAC item → flat record → typed DataFrame |
| `validate.py` | data-quality checks → `ValidationReport` |
| `pull_stac_metadata.py` | CLI entrypoint |

Tests: `python -m pytest`. They need no network access and use a fake client built from [`tests/fixtures/sentinel2_l2a_item.json`](tests/fixtures/sentinel2_l2a_item.json).
