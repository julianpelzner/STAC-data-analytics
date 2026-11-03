from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ingestion import load_to_bigquery as loader
from ingestion.load_to_bigquery import SOURCES, check_dataframe, main, newest_stac_parquet


# --------------------------------------------------------------------------- #
# Fixtures: a tmp "raw" tree with one small valid file per table.
# --------------------------------------------------------------------------- #


def _customers(n=3):
    return pd.DataFrame(
        {
            "customer_id": [f"CUST-{i:05d}" for i in range(n)],
            "company_name": ["Acme"] * n,
            "segment": ["agriculture"] * n,
            "contract_tier": ["self_serve"] * n,
            "country": ["US"] * n,
            "signup_date": ["2026-01-01"] * n,
        }
    )


def _orders(n=4):
    return pd.DataFrame(
        {
            "order_id": [f"ORD-{i:06d}" for i in range(n)],
            "customer_id": ["CUST-00000"] * n,
            "aoi_id": ["AOI-00001"] * n,
            "ordered_at": ["2026-02-01T00:00:00Z"] * n,
            "window_start": ["2026-02-02T00:00:00Z"] * n,
            "window_end": ["2026-02-10T00:00:00Z"] * n,
            "max_cloud_cover": [20.0] * n,
            "priority": ["standard"] * n,
            "price_usd": [100.0] * n,
            "processing_hours": [5.0] * n,
        }
    )


@pytest.fixture
def raw_tree(tmp_path):
    """A tmp raw dir with customers.csv, orders.csv and a stac parquet."""
    synth = tmp_path / "synthetic"
    synth.mkdir()
    _customers().to_csv(synth / "customers.csv", index=False)
    _orders().to_csv(synth / "orders.csv", index=False)

    stac_dir = tmp_path / "stac_items"
    stac_dir.mkdir()
    stac = pd.DataFrame(
        {col: [None, None] for col in SOURCES["stac_items"].expected_columns}
    )
    stac["item_id"] = ["a", "b"]
    stac["region"] = ["sahel", "amazon"]
    stac.to_parquet(stac_dir / "stac_items_20251001_20260930.parquet", index=False)

    return tmp_path


def run(raw_tree, storage, bq, *extra):
    argv = [
        "--synthetic-dir", str(raw_tree / "synthetic"),
        "--stac-dir", str(raw_tree / "stac_items"),
        "--project", "proj",
        "--bucket", "proj-raw",
        "--dataset", "raw",
        "--ingest-date", "2026-11-02",
        *extra,
    ]
    return main(argv, storage_client=storage, bq_client=bq)


# --------------------------------------------------------------------------- #
# DQ unit tests
# --------------------------------------------------------------------------- #


def test_check_dataframe_ok():
    report = check_dataframe(_customers(), SOURCES["customers"])
    assert report.ok and report.row_count == 3


def test_check_dataframe_flags_empty():
    report = check_dataframe(_customers(0), SOURCES["customers"])
    assert not report.ok
    assert any("no rows" in e for e in report.errors)


def test_check_dataframe_flags_missing_column():
    df = _customers().drop(columns=["segment"])
    report = check_dataframe(df, SOURCES["customers"])
    assert not report.ok
    assert any("segment" in e for e in report.errors)


def test_check_dataframe_flags_duplicate_pk():
    df = pd.concat([_customers(1), _customers(1)], ignore_index=True)
    report = check_dataframe(df, SOURCES["customers"])
    assert not report.ok
    assert any("not unique" in e for e in report.errors)


def test_check_dataframe_flags_null_pk():
    df = _customers()
    df.loc[0, "customer_id"] = None
    report = check_dataframe(df, SOURCES["customers"])
    assert not report.ok
    assert any("nulls" in e for e in report.errors)


def test_check_dataframe_composite_pk():
    # Same item_id under two regions is allowed; the PK is (item_id, region).
    df = pd.DataFrame({c: [None, None] for c in SOURCES["stac_items"].expected_columns})
    df["item_id"] = ["x", "x"]
    df["region"] = ["sahel", "amazon"]
    assert check_dataframe(df, SOURCES["stac_items"]).ok

    df["region"] = ["sahel", "sahel"]  # now a true duplicate
    assert not check_dataframe(df, SOURCES["stac_items"]).ok


def test_newest_stac_parquet_picks_latest(tmp_path):
    (tmp_path / "stac_items_20250101_20251231.parquet").touch()
    (tmp_path / "stac_items_20251001_20260930.parquet").touch()
    assert newest_stac_parquet(tmp_path).name == "stac_items_20251001_20260930.parquet"
    assert newest_stac_parquet(tmp_path / "nope") is None


# --------------------------------------------------------------------------- #
# End-to-end with injected fakes
# --------------------------------------------------------------------------- #


def test_main_uploads_and_loads_all(raw_tree, fake_storage_cls, fake_bq_cls):
    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage)

    assert run(raw_tree, storage, bq, "--only", "customers", "orders") == 0

    # Both files uploaded to the date-partitioned path.
    dests = {name for (_, name, _) in storage.uploads}
    assert "synthetic/customers/ingest_date=2026-11-02/customers.csv" in dests
    assert "synthetic/orders/ingest_date=2026-11-02/orders.csv" in dests
    assert all(bucket == "proj-raw" for (bucket, _, _) in storage.uploads)

    loaded_tables = {call["table"] for call in bq.load_calls}
    assert loaded_tables == {"proj.raw.customers", "proj.raw.orders"}

    # CSV loads carry an explicit schema, skip the header, and truncate.
    cust_call = next(c for c in bq.load_calls if c["table"].endswith("customers"))
    jc = cust_call["job_config"]
    assert jc.skip_leading_rows == 1
    assert [f.name for f in jc.schema] == list(SOURCES["customers"].expected_columns)
    assert jc.write_disposition == "WRITE_TRUNCATE"
    assert cust_call["location"] == "US"


def test_main_stac_parquet_autodetects(raw_tree, fake_storage_cls, fake_bq_cls):
    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage)

    assert run(raw_tree, storage, bq, "--only", "stac_items") == 0

    (name,) = [name for (_, name, _) in storage.uploads]
    assert name.startswith("stac_items/ingest_date=2026-11-02/")

    (call,) = bq.load_calls
    jc = call["job_config"]
    assert jc.autodetect is True
    assert jc.schema is None  # parquet brings its own schema


def test_main_dq_failure_loads_nothing(raw_tree, fake_storage_cls, fake_bq_cls):
    # Duplicate a PK so DQ fails for customers.
    dupe = pd.concat([_customers(1), _customers(1)], ignore_index=True)
    dupe.to_csv(raw_tree / "synthetic" / "customers.csv", index=False)

    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage)

    assert run(raw_tree, storage, bq, "--only", "customers", "orders") == 1
    assert storage.uploads == []  # one bad source blocks the whole batch
    assert bq.load_calls == []


def test_main_missing_source_file(raw_tree, fake_storage_cls, fake_bq_cls):
    (raw_tree / "synthetic" / "customers.csv").unlink()
    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage)
    assert run(raw_tree, storage, bq, "--only", "customers") == 1
    assert bq.load_calls == []


def test_main_dry_run_touches_no_gcp(raw_tree):
    # No clients injected: a dry run must not instantiate real GCP clients.
    argv = [
        "--synthetic-dir", str(raw_tree / "synthetic"),
        "--stac-dir", str(raw_tree / "stac_items"),
        "--only", "customers",
        "--dry-run",
    ]
    assert main(argv) == 0


def test_main_row_count_mismatch_fails(raw_tree, fake_storage_cls, fake_bq_cls):
    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage, row_counts={"customers": 999})
    assert run(raw_tree, storage, bq, "--only", "customers") == 1
    assert bq.load_calls  # it did attempt the load


def test_main_no_fail_on_dq_loads_good_sources(raw_tree, fake_storage_cls, fake_bq_cls):
    dupe = pd.concat([_customers(1), _customers(1)], ignore_index=True)
    dupe.to_csv(raw_tree / "synthetic" / "customers.csv", index=False)

    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage)

    # customers fails DQ, orders passes: --no-fail-on-dq loads only orders.
    assert run(raw_tree, storage, bq, "--only", "customers", "orders", "--no-fail-on-dq") == 0
    loaded = {c["table"] for c in bq.load_calls}
    assert loaded == {"proj.raw.orders"}


def test_main_missing_project_config(raw_tree, fake_storage_cls, fake_bq_cls):
    storage = fake_storage_cls()
    bq = fake_bq_cls(storage=storage)
    argv = [
        "--synthetic-dir", str(raw_tree / "synthetic"),
        "--only", "customers",
        "--project", "",
        "--bucket", "",
    ]
    assert main(argv, storage_client=storage, bq_client=bq) == 2
    assert bq.load_calls == []
