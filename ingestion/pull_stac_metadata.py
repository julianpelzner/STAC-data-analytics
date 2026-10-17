"""Pull STAC imagery metadata for the configured AOIs and land it as Parquet.

Usage:
    python -m ingestion.pull_stac_metadata
    python -m ingestion.pull_stac_metadata --regions sahel --start 2026-08-01 --end 2026-09-30
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from pystac_client import Client

from ingestion import config
from ingestion.stac_client import open_client, search_items
from ingestion.transform import item_to_record, records_to_dataframe
from ingestion.validate import ValidationReport, validate_items

logger = logging.getLogger("ingestion.pull_stac_metadata")


def default_window(today: date) -> tuple[date, date]:
    """Trailing 12 full calendar months before `today`."""
    end = today.replace(day=1) - timedelta(days=1)
    month_index = end.year * 12 + (end.month - 1) - 11
    start = date(month_index // 12, month_index % 12 + 1, 1)
    return start, end


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    default_start, default_end = default_window(date.today())
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=date.fromisoformat, default=default_start, help="inclusive, YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, default=default_end, help="inclusive, YYYY-MM-DD")
    parser.add_argument("--regions", nargs="+", help="subset of AOI names from aois.yaml (default: all)")
    parser.add_argument("--collection", default=config.DEFAULT_COLLECTION)
    parser.add_argument("--max-items-per-window", type=int, default=None, help="cap per AOI per month (for testing)")
    parser.add_argument("--aoi-config", type=Path, default=config.AOI_CONFIG_PATH)
    parser.add_argument("--output-dir", type=Path, default=config.RAW_OUTPUT_DIR)
    parser.add_argument("--sample-path", type=Path, default=config.SAMPLE_OUTPUT_PATH)
    parser.add_argument("--sample-size", type=int, default=500, help="0 disables writing the sample")
    parser.add_argument("--report-dir", type=Path, default=config.VALIDATION_REPORT_DIR)
    parser.add_argument("--stac-api-url", default=config.STAC_API_URL)
    parser.add_argument(
        "--no-fail-on-validation",
        action="store_true",
        help="write output and exit 0 even if validation errors are found",
    )
    return parser.parse_args(argv)


def stratified_sample(df: pd.DataFrame, size: int, seed: int = 42) -> pd.DataFrame:
    """Roughly equal rows per region, so the committed sample covers every AOI."""
    per_region = max(1, size // max(1, df["region"].nunique()))
    return (
        df.sample(frac=1, random_state=seed)
        .groupby("region")
        .head(per_region)
        .sort_values(["region", "acquired_at", "item_id"])
        .reset_index(drop=True)
    )


def write_validation_report(
    report: ValidationReport,
    args: argparse.Namespace,
    ingested_at: datetime,
    regions: list[str],
    output_path: Path | None,
) -> Path:
    """Persist one run's validation results; written on failure too, so bad runs can be reviewed."""
    payload = {
        "run_id": ingested_at.isoformat(),
        "stac_api_url": args.stac_api_url,
        "collection": args.collection,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "regions": regions,
        "written": output_path is not None,
        "output_path": str(output_path) if output_path else None,
        **report.to_dict(),
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    path = args.report_dir / f"validation_{args.start:%Y%m%d}_{args.end:%Y%m%d}_{ingested_at:%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(payload, indent=2))
    logger.info("wrote validation report to %s", path)
    return path


def main(argv: list[str] | None = None, client: Client | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)

    aois = config.load_aois(args.aoi_config)
    if args.regions:
        unknown = set(args.regions) - {a.name for a in aois}
        if unknown:
            logger.error("unknown regions: %s", sorted(unknown))
            return 2
        aois = [a for a in aois if a.name in args.regions]

    logger.info(
        "pulling %s for %d AOIs, %s..%s from %s",
        args.collection, len(aois), args.start, args.end, args.stac_api_url,
    )
    client = client or open_client(args.stac_api_url)
    ingested_at = datetime.now(timezone.utc)

    records = []
    for aoi in aois:
        for item in search_items(client, aoi, args.start, args.end, args.collection, args.max_items_per_window):
            records.append(item_to_record(item, aoi.name, args.stac_api_url, ingested_at))

    df = records_to_dataframe(records)

    regions = [a.name for a in aois]
    report = validate_items(df, args.start, args.end, expected_regions=regions)
    for warning in report.warnings:
        logger.warning("validation: %s", warning)
    for error in report.errors:
        logger.error("validation: %s", error)
    if not report.ok and not args.no_fail_on_validation:
        logger.error("validation failed; nothing written")
        write_validation_report(report, args, ingested_at, regions, output_path=None)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.output_dir / f"stac_items_{args.start:%Y%m%d}_{args.end:%Y%m%d}.parquet"
    df.to_parquet(output_path, index=False, compression="snappy")
    logger.info("wrote %d rows to %s", len(df), output_path)
    write_validation_report(report, args, ingested_at, regions, output_path)

    if args.sample_size and not df.empty:
        args.sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample = stratified_sample(df, args.sample_size)
        sample.to_parquet(args.sample_path, index=False, compression="snappy")
        logger.info("wrote %d-row sample to %s", len(sample), args.sample_path)

    if not df.empty:
        summary = df.groupby("region").agg(items=("item_id", "count"), mean_cloud_cover=("cloud_cover", "mean"))
        logger.info("summary:\n%s", summary.round(1).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
