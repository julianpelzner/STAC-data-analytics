"""Flatten STAC items into a tabular schema for the raw landing layer."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import pystac

# Output column -> STAC property key. All optional: missing properties become null.
PROPERTY_COLUMNS = {
    "platform": "platform",
    "constellation": "constellation",
    "mgrs_tile": "s2:mgrs_tile",
    "cloud_cover": "eo:cloud_cover",
    "nodata_pct": "s2:nodata_pixel_percentage",
    "high_proba_clouds_pct": "s2:high_proba_clouds_percentage",
    "medium_proba_clouds_pct": "s2:medium_proba_clouds_percentage",
    "thin_cirrus_pct": "s2:thin_cirrus_percentage",
    "cloud_shadow_pct": "s2:cloud_shadow_percentage",
    "vegetation_pct": "s2:vegetation_percentage",
    "water_pct": "s2:water_percentage",
    "snow_ice_pct": "s2:snow_ice_percentage",
    "solar_zenith": "s2:mean_solar_zenith",
    "solar_azimuth": "s2:mean_solar_azimuth",
    "relative_orbit": "sat:relative_orbit",
    "orbit_state": "sat:orbit_state",
    "processing_baseline": "s2:processing_baseline",
}

FLOAT_COLUMNS = [
    "cloud_cover",
    "nodata_pct",
    "high_proba_clouds_pct",
    "medium_proba_clouds_pct",
    "thin_cirrus_pct",
    "cloud_shadow_pct",
    "vegetation_pct",
    "water_pct",
    "snow_ice_pct",
    "solar_zenith",
    "solar_azimuth",
    "bbox_minx",
    "bbox_miny",
    "bbox_maxx",
    "bbox_maxy",
    "centroid_lon",
    "centroid_lat",
]

STRING_COLUMNS = [
    "item_id",
    "collection",
    "region",
    "platform",
    "constellation",
    "mgrs_tile",
    "orbit_state",
    "processing_baseline",
    "geometry_geojson",
    "stac_api_url",
]

COLUMNS = [
    "item_id",
    "collection",
    "region",
    "acquired_at",
    "acquired_date",
    *PROPERTY_COLUMNS,
    "bbox_minx",
    "bbox_miny",
    "bbox_maxx",
    "bbox_maxy",
    "centroid_lon",
    "centroid_lat",
    "geometry_geojson",
    "stac_api_url",
    "ingested_at",
]


def item_to_record(
    item: pystac.Item,
    region: str,
    stac_api_url: str,
    ingested_at: datetime,
) -> dict:
    props = item.properties
    acquired_at = item.datetime or pd.to_datetime(props.get("start_datetime"), utc=True)
    if acquired_at is not None and acquired_at.tzinfo is None:
        acquired_at = acquired_at.replace(tzinfo=timezone.utc)

    minx, miny, maxx, maxy = item.bbox if item.bbox else (None, None, None, None)
    has_bbox = item.bbox is not None

    record = {
        "item_id": item.id,
        "collection": item.collection_id,
        "region": region,
        "acquired_at": acquired_at,
        "acquired_date": acquired_at.date() if acquired_at is not None else None,
        **{col: props.get(key) for col, key in PROPERTY_COLUMNS.items()},
        "bbox_minx": minx,
        "bbox_miny": miny,
        "bbox_maxx": maxx,
        "bbox_maxy": maxy,
        "centroid_lon": (minx + maxx) / 2 if has_bbox else None,
        "centroid_lat": (miny + maxy) / 2 if has_bbox else None,
        "geometry_geojson": json.dumps(item.geometry) if item.geometry else None,
        "stac_api_url": stac_api_url,
        "ingested_at": ingested_at,
    }
    return record


def records_to_dataframe(records: list[dict]) -> pd.DataFrame:
    """Build a typed DataFrame, keeping one row per (item_id, region)."""
    df = pd.DataFrame.from_records(records, columns=COLUMNS)

    for col in FLOAT_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["relative_orbit"] = pd.to_numeric(df["relative_orbit"], errors="coerce").astype("Int64")
    for col in STRING_COLUMNS:
        df[col] = df[col].astype("string")
    df["acquired_at"] = pd.to_datetime(df["acquired_at"], utc=True)
    df["acquired_date"] = pd.to_datetime(df["acquired_date"]).dt.date
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)

    return (
        df.drop_duplicates(subset=["item_id", "region"], keep="first")
        .sort_values(["region", "acquired_at", "item_id"])
        .reset_index(drop=True)
    )
