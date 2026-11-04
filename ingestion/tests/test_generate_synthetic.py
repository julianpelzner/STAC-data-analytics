"""Invariants for the synthetic generator — the data_model.md §7 rules.

Generation is pure and offline, so these run the real shipped config against a
fixed window and assert on the resulting tables. One CLI test covers the I/O.
"""

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from shapely import wkt
from shapely.geometry import box

from ingestion import config
from ingestion.generate_synthetic import generate, load_config, main

START = date(2025, 1, 1)
END = date(2025, 12, 31)
SEED = 42


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config(config.SYNTHETIC_CONFIG_PATH)


@pytest.fixture(scope="module")
def aois_cfg():
    return config.load_aois()


@pytest.fixture(scope="module")
def bboxes(aois_cfg):
    return {a.name: a.bbox for a in aois_cfg}


@pytest.fixture(scope="module")
def tables(cfg, aois_cfg):
    return generate(cfg, aois_cfg, SEED, START, END)


# --- Keys and referential integrity ------------------------------------ #


def test_primary_keys_unique_and_present(tables):
    for name, key in [("customers", "customer_id"), ("aois", "aoi_id"), ("orders", "order_id")]:
        col = tables[name][key]
        assert col.notna().all(), f"{name}.{key} has nulls"
        assert col.is_unique, f"{name}.{key} not unique"


def test_foreign_keys_resolve(tables):
    customers, aois, orders = tables["customers"], tables["aois"], tables["orders"]
    assert aois["customer_id"].isin(customers["customer_id"]).all()
    assert orders["aoi_id"].isin(aois["aoi_id"]).all()


def test_order_customer_matches_aoi_owner(tables):
    merged = tables["orders"].merge(
        tables["aois"][["aoi_id", "customer_id"]], on="aoi_id", suffixes=("", "_aoi")
    )
    assert (merged["customer_id"] == merged["customer_id_aoi"]).all()


# --- Allowed value sets ------------------------------------------------- #


def test_categoricals_in_allowed_sets(tables, cfg, bboxes):
    assert set(tables["customers"]["segment"]) <= set(cfg["segments"])
    assert set(tables["customers"]["contract_tier"]) <= {"self_serve", "professional", "enterprise"}
    assert set(tables["orders"]["priority"]) <= {"standard", "rush"}
    assert set(tables["aois"]["shape_type"]) <= set(cfg["shapes"])
    assert set(tables["aois"]["region"]) <= set(bboxes)


# --- Geometry ----------------------------------------------------------- #


def test_geometry_valid_and_inside_region_bbox(tables, bboxes):
    for _, row in tables["aois"].iterrows():
        poly = wkt.loads(row["geometry_wkt"])
        assert poly.is_valid, f"{row['aoi_id']} invalid geometry"
        region_box = box(*bboxes[row["region"]])
        assert region_box.buffer(1e-9).covers(poly), f"{row['aoi_id']} escapes {row['region']} bbox"


def test_geometry_metrics_in_range(tables):
    aois = tables["aois"]
    assert (aois["area_km2"] > 0).all()
    assert ((aois["compactness"] > 0) & (aois["compactness"] <= 1)).all()
    assert (aois["vertex_count"] >= 3).all()


def test_compactness_separates_by_shape(tables):
    """The whole point of the shapes block: corridors are far less compact than
    admin blocks. Guards against the shape knobs silently collapsing."""
    means = tables["aois"].groupby("shape_type")["compactness"].mean()
    assert means["corridor"] < 0.35
    assert means["admin_block"] > 0.6
    assert means["corridor"] < means["admin_block"]


# --- Dates and the ordering chain --------------------------------------- #


def test_date_chain_holds(tables):
    customers = tables["customers"].set_index("customer_id")
    aois = tables["aois"].set_index("aoi_id")
    orders = tables["orders"]

    assert (orders["ordered_at"] <= orders["window_start"]).all()
    assert (orders["window_start"] < orders["window_end"]).all()

    created = aois.loc[orders["aoi_id"], "created_at"].to_numpy()
    assert (created <= orders["ordered_at"].to_numpy()).all()

    signup = pd.to_datetime(customers.loc[aois["customer_id"], "signup_date"].to_numpy())
    assert (signup <= pd.to_datetime(aois["created_at"].to_numpy()).tz_localize(None)).all()


def test_windows_within_stac_range(tables):
    lo = datetime(START.year, START.month, START.day, tzinfo=UTC)
    hi = datetime(END.year, END.month, END.day, tzinfo=UTC) + pd.Timedelta(days=1)
    orders = tables["orders"]
    assert (orders["window_start"] >= lo).all()
    assert (orders["window_end"] <= hi).all()


# --- Pricing and processing --------------------------------------------- #


def test_price_and_processing_positive(tables, cfg):
    assert (tables["orders"]["price_usd"] >= cfg["pricing"]["min_order_fee"]).all()
    assert (tables["orders"]["processing_hours"] > 0).all()


def test_max_cloud_cover_matches_segment(tables, cfg):
    merged = (
        tables["orders"][["aoi_id", "max_cloud_cover"]]
        .merge(tables["aois"][["aoi_id", "customer_id"]], on="aoi_id")
        .merge(tables["customers"][["customer_id", "segment"]], on="customer_id")
    )
    for segment, group in merged.groupby("segment"):
        assert (group["max_cloud_cover"] == cfg["segments"][segment]["cloud_max"]).all()


# --- Determinism -------------------------------------------------------- #


def test_same_seed_identical_output(cfg, aois_cfg, tables):
    again = generate(cfg, aois_cfg, SEED, START, END)
    for name in tables:
        pd.testing.assert_frame_equal(tables[name], again[name])


def test_different_seed_differs(cfg, aois_cfg, tables):
    other = generate(cfg, aois_cfg, SEED + 1, START, END)
    assert not tables["aois"]["geometry_wkt"].equals(other["aois"]["geometry_wkt"])


# --- CLI / I/O ---------------------------------------------------------- #


def test_main_writes_tables_and_referential_sample(tmp_path):
    out_dir, sample_dir = tmp_path / "raw", tmp_path / "sample"
    argv = [
        "--seed",
        str(SEED),
        "--start",
        START.isoformat(),
        "--end",
        END.isoformat(),
        "--output-dir",
        str(out_dir),
        "--sample-dir",
        str(sample_dir),
        "--sample-customers",
        "10",
    ]
    assert main(argv) == 0

    for name in ("customers", "aois", "orders"):
        assert (out_dir / f"{name}.csv").exists()

    customers = pd.read_csv(sample_dir / "customers.csv")
    aois = pd.read_csv(sample_dir / "aois.csv")
    orders = pd.read_csv(sample_dir / "orders.csv")
    assert len(customers) == 10
    assert aois["customer_id"].isin(customers["customer_id"]).all()
    assert orders["aoi_id"].isin(aois["aoi_id"]).all()


def test_main_rejects_inverted_window(tmp_path):
    argv = ["--start", "2025-12-31", "--end", "2025-01-01", "--output-dir", str(tmp_path)]
    assert main(argv) == 2
