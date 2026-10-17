from datetime import date

import pytest

from ingestion.tests.conftest import INGESTED_AT, STAC_URL
from ingestion.transform import item_to_record, records_to_dataframe
from ingestion.validate import validate_items

START, END = date(2026, 8, 1), date(2026, 8, 31)


@pytest.fixture
def clean_df(make_item):
    records = [
        item_to_record(make_item(f"item-{i}", f"2026-08-{10 + i}T10:00:00Z"), "sahel", STAC_URL, INGESTED_AT)
        for i in range(5)
    ]
    return records_to_dataframe(records)


def test_clean_data_passes(clean_df):
    report = validate_items(clean_df, START, END, expected_regions=["sahel"])
    assert report.ok, report.errors
    assert report.warnings == []
    assert report.row_count == 5
    assert report.rows_by_region == {"sahel": 5}
    assert report.null_cloud_cover_rate_by_region == {"sahel": 0.0}


def test_empty_dataframe_fails():
    report = validate_items(records_to_dataframe([]))
    assert not report.ok
    assert "no items" in report.errors[0]


def test_missing_column_fails(clean_df):
    report = validate_items(clean_df.drop(columns=["cloud_cover"]))
    assert any("missing required columns" in e for e in report.errors)


def test_null_required_field_fails(clean_df):
    clean_df.loc[0, "geometry_geojson"] = None
    report = validate_items(clean_df)
    assert any("geometry_geojson" in e for e in report.errors)


def test_duplicates_fail(clean_df):
    import pandas as pd

    report = validate_items(pd.concat([clean_df, clean_df.iloc[[0]]]))
    assert any("duplicate" in e for e in report.errors)


def test_cloud_cover_out_of_range_fails(clean_df):
    clean_df.loc[0, "cloud_cover"] = 120.0
    report = validate_items(clean_df)
    assert any("cloud_cover" in e for e in report.errors)


def test_high_null_cloud_cover_warns(clean_df):
    clean_df.loc[0, "cloud_cover"] = None
    report = validate_items(clean_df)
    assert report.ok
    assert any("cloud_cover" in w and "'sahel'" in w for w in report.warnings)


def test_null_cloud_cover_checked_per_region(make_item):
    # 1 null in 35 rows overall (~2.9%) stays under the threshold; sahel alone is 20%.
    records = [
        item_to_record(make_item(f"amazon-{i}", f"2026-08-{1 + i:02d}T10:00:00Z"), "amazon", STAC_URL, INGESTED_AT)
        for i in range(30)
    ]
    records += [
        item_to_record(
            make_item(f"sahel-{i}", f"2026-08-{10 + i}T10:00:00Z", drop=("eo:cloud_cover",) if i == 0 else ()),
            "sahel",
            STAC_URL,
            INGESTED_AT,
        )
        for i in range(5)
    ]
    report = validate_items(records_to_dataframe(records), START, END)
    assert report.ok, report.errors
    cloud_warnings = [w for w in report.warnings if "cloud_cover" in w]
    assert len(cloud_warnings) == 1
    assert "'sahel'" in cloud_warnings[0]
    assert "amazon" not in cloud_warnings[0]


def test_acquired_outside_window_fails(clean_df):
    report = validate_items(clean_df, date(2026, 8, 12), END)
    assert any("outside" in e for e in report.errors)


def test_invalid_bbox_fails(clean_df):
    clean_df.loc[0, "bbox_miny"] = 95.0
    report = validate_items(clean_df)
    assert any("bbox" in e for e in report.errors)


def test_missing_region_warns(clean_df):
    report = validate_items(clean_df, expected_regions=["sahel", "amazon"])
    assert report.ok
    assert any("amazon" in w for w in report.warnings)
    assert report.rows_by_region["amazon"] == 0
