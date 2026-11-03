"""Runtime configuration: environment variables, paths, and AOI definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_DIR = REPO_ROOT / "ingestion"

load_dotenv(REPO_ROOT / ".env")

DEFAULT_STAC_API_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
STAC_API_URL = os.getenv("STAC_API_URL", DEFAULT_STAC_API_URL)
DEFAULT_COLLECTION = "sentinel-2-l2a"

AOI_CONFIG_PATH = INGESTION_DIR / "aois.yaml"
RAW_OUTPUT_DIR = REPO_ROOT / "data" / "raw" / "stac_items"
SAMPLE_OUTPUT_PATH = REPO_ROOT / "data" / "raw_sample" / "stac_items_sample.parquet"
VALIDATION_REPORT_DIR = REPO_ROOT / "data" / "raw" / "validation_reports"

# Synthetic business-data generator (see docs/data_model.md).
SYNTHETIC_CONFIG_PATH = INGESTION_DIR / "synthetic" / "config.yaml"
SYNTHETIC_RAW_DIR = REPO_ROOT / "data" / "raw" / "synthetic"
SYNTHETIC_SAMPLE_DIR = REPO_ROOT / "data" / "raw_sample"

# GCS + BigQuery raw landing (see ingestion/load_to_bigquery.py, docs/gcp_setup.md).
# Required at load time, not at import, so the project still runs offline.
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
BIGQUERY_RAW_DATASET = os.getenv("BIGQUERY_RAW_DATASET", "raw")
BIGQUERY_LOCATION = os.getenv("BIGQUERY_LOCATION", "US")


@dataclass(frozen=True)
class AOI:
    name: str
    bbox: tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)
    description: str = ""


def load_aois(path: Path = AOI_CONFIG_PATH) -> list[AOI]:
    """Load the regional AOIs from YAML."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    aois = []
    for entry in raw["aois"]:
        bbox = tuple(float(v) for v in entry["bbox"])
        if len(bbox) != 4:
            raise ValueError(f"AOI {entry['name']!r} bbox must have 4 values, got {len(bbox)}")
        aois.append(AOI(name=entry["name"], bbox=bbox, description=entry.get("description", "")))
    return aois
