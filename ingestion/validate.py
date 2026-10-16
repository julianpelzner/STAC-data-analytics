"""Data-quality checks on the flattened STAC item table before it is landed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ingestion.transform import COLUMNS

NOT_NULL_COLUMNS = ["item_id", "region", "acquired_at", "geometry_geojson"]
# Per-region warn threshold. S2 L2A populates eo:cloud_cover on ~all items
# (0 nulls across 4,593 items / 6 regions, Oct 2025–Sep 2026 pull), so >5% in a
# region points to a schema/source change rather than stray items.
NULL_CLOUD_COVER_WARN_RATE = 0.05


@dataclass
class ValidationReport:
    row_count: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_items(
    df: pd.DataFrame,
    start: date | None = None,
    end: date | None = None,
    expected_regions: list[str] | None = None,
) -> ValidationReport:
    report = ValidationReport(row_count=len(df))

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        report.errors.append(f"missing required columns: {missing}")
        return report

    if df.empty:
        report.errors.append("no items returned")
        return report

    for col in NOT_NULL_COLUMNS:
        n = int(df[col].isna().sum())
        if n:
            report.errors.append(f"{col}: {n} null values")

    dupes = int(df.duplicated(subset=["item_id", "region"]).sum())
    if dupes:
        report.errors.append(f"{dupes} duplicate (item_id, region) rows")

    cloud = df["cloud_cover"]
    out_of_range = int(((cloud < 0) | (cloud > 100)).sum())
    if out_of_range:
        report.errors.append(f"cloud_cover: {out_of_range} values outside [0, 100]")

    null_cloud_by_region = cloud.isna().groupby(df["region"]).mean()
    for region, rate in null_cloud_by_region.items():
        if rate > NULL_CLOUD_COVER_WARN_RATE:
            report.warnings.append(f"cloud_cover: {rate:.1%} null in region {region!r}")

    if start is not None and end is not None:
        acquired = df["acquired_at"].dt.date
        outside = int(((acquired < start) | (acquired > end)).sum())
        if outside:
            report.errors.append(f"acquired_at: {outside} rows outside {start}..{end}")

    bad_bbox = (
        (df["bbox_minx"] > df["bbox_maxx"])
        | (df["bbox_miny"] > df["bbox_maxy"])
        | (df["bbox_minx"] < -180)
        | (df["bbox_maxx"] > 180)
        | (df["bbox_miny"] < -90)
        | (df["bbox_maxy"] > 90)
    )
    n_bad_bbox = int(bad_bbox.sum())
    if n_bad_bbox:
        report.errors.append(f"bbox: {n_bad_bbox} invalid bounding boxes")

    if expected_regions:
        present = set(df["region"].dropna())
        for region in expected_regions:
            if region not in present:
                report.warnings.append(f"region {region!r} returned 0 items")

    return report
