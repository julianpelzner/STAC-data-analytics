import json
from datetime import date

import pandas as pd
import pytest

from ingestion.pull_stac_metadata import default_window, main
from ingestion.transform import COLUMNS


@pytest.fixture
def aoi_config(tmp_path):
    path = tmp_path / "aois.yaml"
    path.write_text(
        "aois:\n"
        "  - name: sahel\n    bbox: [1.5, 13.0, 2.5, 14.0]\n"
        "  - name: amazon\n    bbox: [-52.5, -4.0, -51.5, -3.0]\n"
    )
    return path


def run(tmp_path, aoi_config, client, *extra):
    argv = [
        "--start", "2026-07-01",
        "--end", "2026-08-31",
        "--aoi-config", str(aoi_config),
        "--output-dir", str(tmp_path / "raw"),
        "--sample-path", str(tmp_path / "sample" / "sample.parquet"),
        "--sample-size", "2",
        "--report-dir", str(tmp_path / "reports"),
        *extra,
    ]
    return main(argv, client=client)


def read_report(tmp_path):
    (path,) = (tmp_path / "reports").glob("validation_*.json")
    return json.loads(path.read_text())


def test_default_window_is_trailing_12_full_months():
    assert default_window(date(2026, 10, 14)) == (date(2025, 10, 1), date(2026, 9, 30))
    assert default_window(date(2026, 1, 1)) == (date(2025, 1, 1), date(2025, 12, 31))


def test_main_writes_parquet_and_sample(tmp_path, aoi_config, make_item, fake_client_cls):
    items = [make_item(f"item-{i}", f"2026-0{7 + i % 2}-1{i}T10:00:00Z") for i in range(4)]
    client = fake_client_cls(items)

    assert run(tmp_path, aoi_config, client) == 0

    out = tmp_path / "raw" / "stac_items_20260701_20260831.parquet"
    df = pd.read_parquet(out)
    assert list(df.columns) == COLUMNS
    # Fake client ignores bbox, so every item lands in both AOIs.
    assert len(df) == 8
    assert set(df["region"]) == {"sahel", "amazon"}

    sample = pd.read_parquet(tmp_path / "sample" / "sample.parquet")
    assert len(sample) == 2
    assert set(sample["region"]) == {"sahel", "amazon"}

    report = read_report(tmp_path)
    assert report["ok"] and report["written"]
    assert report["output_path"] == str(out)
    assert report["row_count"] == 8
    assert report["rows_by_region"] == {"amazon": 4, "sahel": 4}


def test_main_region_subset(tmp_path, aoi_config, make_item, fake_client_cls):
    client = fake_client_cls([make_item("a", "2026-07-15T10:00:00Z")])
    assert run(tmp_path, aoi_config, client, "--regions", "sahel") == 0
    df = pd.read_parquet(next((tmp_path / "raw").glob("*.parquet")))
    assert set(df["region"]) == {"sahel"}


def test_main_unknown_region(tmp_path, aoi_config, fake_client_cls):
    assert run(tmp_path, aoi_config, fake_client_cls([]), "--regions", "atlantis") == 2


def test_main_validation_failure_writes_nothing(tmp_path, aoi_config, make_item, fake_client_cls):
    client = fake_client_cls([make_item("a", "2026-07-15T10:00:00Z", properties={"eo:cloud_cover": 150})])
    assert run(tmp_path, aoi_config, client) == 1
    assert not (tmp_path / "raw").exists()

    report = read_report(tmp_path)
    assert not report["ok"] and not report["written"]
    assert report["output_path"] is None
    assert any("cloud_cover" in e for e in report["errors"])


def test_main_no_fail_on_validation(tmp_path, aoi_config, make_item, fake_client_cls):
    client = fake_client_cls([make_item("a", "2026-07-15T10:00:00Z", properties={"eo:cloud_cover": 150})])
    assert run(tmp_path, aoi_config, client, "--no-fail-on-validation") == 0
    assert any((tmp_path / "raw").glob("*.parquet"))

    report = read_report(tmp_path)
    assert not report["ok"] and report["written"]
