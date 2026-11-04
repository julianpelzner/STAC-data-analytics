"""Land the local raw files into GCS and BigQuery `raw` tables.

Each source file is uploaded to a Hive-style, date-partitioned GCS path
(`gs://<bucket>/<prefix>/ingest_date=YYYY-MM-DD/<file>`), so every run is
immutable and replayable, and then loaded into `raw.<table>` with
`WRITE_TRUNCATE` so the table stays 1:1 with the latest file.

Basic data-quality checks run on the local file *before* anything is uploaded
and block the whole load on error (nothing is loaded if any source fails, like
`pull_stac_metadata`'s validation gate). A post-load row-count check confirms
BigQuery ingested exactly as many rows as the source held.

Usage:
    python -m ingestion.load_to_bigquery                      # all four tables
    python -m ingestion.load_to_bigquery --only customers orders
    python -m ingestion.load_to_bigquery --dry-run            # DQ only, no GCP
    python -m ingestion.load_to_bigquery --ingest-date 2026-11-02
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
from google.cloud import bigquery, storage

from ingestion import config, transform
from ingestion.transform import COLUMNS as STAC_COLUMNS

logger = logging.getLogger("ingestion.load_to_bigquery")

_SF = bigquery.SchemaField


def _stac_schema() -> list[bigquery.SchemaField]:
    """Derive the stac_items BigQuery schema from transform.py's dtype lists.

    Single source of truth: adding a column to transform.COLUMNS (and the right
    dtype list) flows through here automatically, the same way validate.py keys
    off COLUMNS. Loading with this explicit schema — rather than autodetect —
    means a type regression in the Parquet writer (e.g. nanosecond timestamps,
    which BigQuery cannot store) fails the load loudly instead of silently
    landing as INT64.
    """
    timestamps = {"acquired_at", "ingested_at"}
    dates = {"acquired_date"}
    integers = {"relative_orbit"}
    schema = []
    for col in STAC_COLUMNS:
        if col in timestamps:
            bq_type = "TIMESTAMP"
        elif col in dates:
            bq_type = "DATE"
        elif col in integers:
            bq_type = "INTEGER"
        elif col in transform.FLOAT_COLUMNS:
            bq_type = "FLOAT"
        elif col in transform.STRING_COLUMNS:
            bq_type = "STRING"
        else:  # pragma: no cover - guards against an untyped new column
            raise ValueError(f"no BigQuery type mapping for stac_items column {col!r}")
        schema.append(_SF(col, bq_type))
    return schema


STAC_SCHEMA = _stac_schema()

# Explicit BigQuery schemas for the CSV synthetic tables keep the load
# deterministic and 1:1 with docs/data_model.md §3. `stac_items` is Parquet, so
# its types are carried by the file itself and BigQuery autodetects them.
CUSTOMERS_SCHEMA = [
    _SF("customer_id", "STRING"),
    _SF("company_name", "STRING"),
    _SF("segment", "STRING"),
    _SF("contract_tier", "STRING"),
    _SF("country", "STRING"),
    _SF("signup_date", "DATE"),
]

AOIS_SCHEMA = [
    _SF("aoi_id", "STRING"),
    _SF("customer_id", "STRING"),
    _SF("aoi_name", "STRING"),
    _SF("region", "STRING"),
    _SF("shape_type", "STRING"),
    _SF("geometry_wkt", "STRING"),
    _SF("area_km2", "FLOAT"),
    _SF("vertex_count", "INTEGER"),
    _SF("compactness", "FLOAT"),
    _SF("created_at", "TIMESTAMP"),
]

ORDERS_SCHEMA = [
    _SF("order_id", "STRING"),
    _SF("customer_id", "STRING"),
    _SF("aoi_id", "STRING"),
    _SF("ordered_at", "TIMESTAMP"),
    _SF("window_start", "TIMESTAMP"),
    _SF("window_end", "TIMESTAMP"),
    _SF("max_cloud_cover", "FLOAT"),
    _SF("priority", "STRING"),
    _SF("price_usd", "FLOAT"),
    _SF("processing_hours", "FLOAT"),
]


@dataclass(frozen=True)
class Source:
    """One raw table: where it comes from locally, where it lands, how it loads."""

    table: str  # BigQuery table name and the GCS leaf prefix
    gcs_prefix: str  # path under the bucket, before `ingest_date=.../`
    source_format: str  # "PARQUET" or "CSV"
    pk: tuple[str, ...]  # columns that must be unique and non-null
    expected_columns: tuple[str, ...]  # DQ: every one must be present
    schema: list[bigquery.SchemaField] | None = None  # None => autodetect (Parquet)


SOURCES: dict[str, Source] = {
    "stac_items": Source(
        table="stac_items",
        gcs_prefix="stac_items",
        source_format="PARQUET",
        pk=("item_id", "region"),
        expected_columns=tuple(STAC_COLUMNS),
        schema=STAC_SCHEMA,
    ),
    "customers": Source(
        table="customers",
        gcs_prefix="synthetic/customers",
        source_format="CSV",
        pk=("customer_id",),
        expected_columns=tuple(f.name for f in CUSTOMERS_SCHEMA),
        schema=CUSTOMERS_SCHEMA,
    ),
    "aois": Source(
        table="aois",
        gcs_prefix="synthetic/aois",
        source_format="CSV",
        pk=("aoi_id",),
        expected_columns=tuple(f.name for f in AOIS_SCHEMA),
        schema=AOIS_SCHEMA,
    ),
    "orders": Source(
        table="orders",
        gcs_prefix="synthetic/orders",
        source_format="CSV",
        pk=("order_id",),
        expected_columns=tuple(f.name for f in ORDERS_SCHEMA),
        schema=ORDERS_SCHEMA,
    ),
}


@dataclass
class DQReport:
    table: str
    row_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------- #
# Local source resolution
# --------------------------------------------------------------------------- #


def newest_stac_parquet(raw_dir: Path) -> Path | None:
    """The most recent full STAC pull in `raw_dir` (filenames sort by window)."""
    matches = sorted(raw_dir.glob("stac_items_*.parquet"))
    return matches[-1] if matches else None


def resolve_path(table: str, args: argparse.Namespace) -> Path | None:
    if table == "stac_items":
        return args.stac_path or newest_stac_parquet(args.stac_dir)
    return args.synthetic_dir / f"{table}.csv"


def read_source(path: Path, source_format: str) -> pd.DataFrame:
    if source_format == "PARQUET":
        return pd.read_parquet(path)
    return pd.read_csv(path)


# --------------------------------------------------------------------------- #
# Data quality
# --------------------------------------------------------------------------- #


def check_dataframe(df: pd.DataFrame, source: Source) -> DQReport:
    """Pre-load checks: non-empty, all columns present, PK unique and non-null."""
    report = DQReport(table=source.table, row_count=len(df))

    if df.empty:
        report.errors.append("source has no rows")

    missing = [c for c in source.expected_columns if c not in df.columns]
    if missing:
        report.errors.append(f"missing columns: {missing}")

    present_pk = [c for c in source.pk if c in df.columns]
    if present_pk:
        nulls = {c for c in present_pk if df[c].isna().any()}
        if nulls:
            report.errors.append(f"primary key has nulls: {sorted(nulls)}")
        dupes = int(df.duplicated(subset=present_pk).sum())
        if dupes:
            report.errors.append(
                f"primary key {list(source.pk)} not unique: {dupes} duplicate row(s)"
            )

    return report


# --------------------------------------------------------------------------- #
# GCS + BigQuery
# --------------------------------------------------------------------------- #


def upload_to_gcs(
    storage_client: storage.Client,
    bucket_name: str,
    local_path: Path,
    dest_blob: str,
) -> str:
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(dest_blob)
    blob.upload_from_filename(str(local_path))
    uri = f"gs://{bucket_name}/{dest_blob}"
    logger.info("uploaded %s -> %s", local_path.name, uri)
    return uri


def load_from_gcs(
    bq_client: bigquery.Client,
    table_id: str,
    gcs_uri: str,
    source: Source,
    location: str,
) -> int:
    """Load one GCS object into `table_id`, replacing it. Returns rows loaded."""
    # Every table loads with an explicit schema (never autodetect), so a type
    # the warehouse can't store fails the load instead of landing silently
    # mistyped. Parquet matches the schema by column name; CSV by position.
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=source.schema,
    )
    if source.source_format == "PARQUET":
        job_config.source_format = bigquery.SourceFormat.PARQUET
    else:
        job_config.source_format = bigquery.SourceFormat.CSV
        job_config.skip_leading_rows = 1  # header row

    job = bq_client.load_table_from_uri(gcs_uri, table_id, job_config=job_config, location=location)
    job.result()  # wait for completion; raises on load failure
    loaded = job.output_rows
    logger.info("loaded %s rows into %s", loaded, table_id)
    return loaded


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only", nargs="+", choices=list(SOURCES), help="subset of tables (default: all)"
    )
    parser.add_argument(
        "--ingest-date",
        type=date.fromisoformat,
        default=date.today(),
        help="partition date, YYYY-MM-DD",
    )
    parser.add_argument(
        "--stac-dir",
        type=Path,
        default=config.RAW_OUTPUT_DIR,
        help="where to find the STAC parquet",
    )
    parser.add_argument(
        "--stac-path", type=Path, default=None, help="explicit STAC parquet (overrides --stac-dir)"
    )
    parser.add_argument(
        "--synthetic-dir",
        type=Path,
        default=config.SYNTHETIC_RAW_DIR,
        help="where the synthetic CSVs live",
    )
    parser.add_argument("--project", default=config.GCP_PROJECT_ID, help="GCP project id")
    parser.add_argument("--bucket", default=config.GCS_BUCKET_NAME, help="GCS landing bucket")
    parser.add_argument(
        "--dataset", default=config.BIGQUERY_RAW_DATASET, help="BigQuery raw dataset"
    )
    parser.add_argument(
        "--location", default=config.BIGQUERY_LOCATION, help="BigQuery/GCS location"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="run DQ checks only; touch no GCP resources"
    )
    parser.add_argument(
        "--no-fail-on-dq",
        action="store_true",
        help="load anyway even if data-quality checks fail",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    storage_client: storage.Client | None = None,
    bq_client: bigquery.Client | None = None,
) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = parse_args(argv)

    tables = args.only or list(SOURCES)

    # Phase 1: read every source and run DQ before touching GCP, so one bad file
    # stops the whole batch instead of leaving half the tables loaded.
    prepared: list[tuple[Source, Path, pd.DataFrame]] = []
    dq_failed = False
    for table in tables:
        source = SOURCES[table]
        path = resolve_path(table, args)
        if path is None or not path.exists():
            logger.error("[%s] source file not found: %s", table, path)
            dq_failed = True
            continue
        df = read_source(path, source.source_format)
        report = check_dataframe(df, source)
        for warning in report.warnings:
            logger.warning("[%s] %s", table, warning)
        for error in report.errors:
            logger.error("[%s] %s", table, error)
        if not report.ok:
            dq_failed = True
        else:
            logger.info("[%s] DQ ok: %d rows from %s", table, report.row_count, path)
        prepared.append((source, path, df))

    if dq_failed and not args.no_fail_on_dq:
        logger.error("data-quality checks failed; nothing loaded")
        return 1

    if args.dry_run:
        logger.info("dry run: %d source(s) checked, nothing uploaded or loaded", len(prepared))
        return 0

    # Only the readable, DQ-passing sources get loaded (relevant with --no-fail-on-dq).
    loadable = [(s, p, df) for (s, p, df) in prepared if check_dataframe(df, s).ok]
    if not loadable:
        logger.error("no loadable sources; nothing to do")
        return 1

    missing_cfg = [
        name for name, val in (("--project", args.project), ("--bucket", args.bucket)) if not val
    ]
    if missing_cfg:
        logger.error(
            "missing required config: %s (set GCP_PROJECT_ID / GCS_BUCKET_NAME or pass flags)",
            missing_cfg,
        )
        return 2

    storage_client = storage_client or storage.Client(project=args.project)
    bq_client = bq_client or bigquery.Client(project=args.project)

    load_failed = False
    for source, path, df in loadable:
        dest_blob = f"{source.gcs_prefix}/ingest_date={args.ingest_date:%Y-%m-%d}/{path.name}"
        uri = upload_to_gcs(storage_client, args.bucket, path, dest_blob)
        table_id = f"{args.project}.{args.dataset}.{source.table}"
        loaded = load_from_gcs(bq_client, table_id, uri, source, args.location)
        if loaded != len(df):
            logger.error(
                "[%s] row-count mismatch: source %d, loaded %s", source.table, len(df), loaded
            )
            load_failed = True

    return 1 if load_failed else 0


if __name__ == "__main__":
    sys.exit(main())
