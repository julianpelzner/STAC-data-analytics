import json
from datetime import date

import pandas as pd

from ingestion.tests.conftest import INGESTED_AT, STAC_URL
from ingestion.transform import COLUMNS, item_to_record, records_to_dataframe


def test_item_to_record_maps_fields(make_item):
    record = item_to_record(make_item(), "sahel", STAC_URL, INGESTED_AT)

    assert set(record) == set(COLUMNS)
    assert record["item_id"] == "S2A_MSIL2A_20260830T103601_R065_T31PCR_20260830T200111"
    assert record["collection"] == "sentinel-2-l2a"
    assert record["region"] == "sahel"
    assert record["acquired_date"] == date(2026, 8, 30)
    assert record["acquired_at"].tzinfo is not None
    assert record["platform"] == "Sentinel-2A"
    assert record["mgrs_tile"] == "31PCR"
    assert record["cloud_cover"] == 18.311757
    assert record["high_proba_clouds_pct"] == 6.518484
    assert record["relative_orbit"] == 65
    assert record["orbit_state"] == "descending"
    assert record["bbox_minx"] == 1.1444793
    assert abs(record["centroid_lat"] - (13.4732217 + 14.4696562) / 2) < 1e-9
    assert json.loads(record["geometry_geojson"])["type"] == "Polygon"
    assert record["stac_api_url"] == STAC_URL


def test_missing_optional_properties_become_none(make_item):
    item = make_item(drop=["eo:cloud_cover", "s2:mgrs_tile", "sat:relative_orbit"])
    record = item_to_record(item, "sahel", STAC_URL, INGESTED_AT)
    assert record["cloud_cover"] is None
    assert record["mgrs_tile"] is None
    assert record["relative_orbit"] is None


def test_records_to_dataframe_types_and_dedupe(make_item):
    records = [
        item_to_record(make_item("a"), "sahel", STAC_URL, INGESTED_AT),
        item_to_record(make_item("a"), "sahel", STAC_URL, INGESTED_AT),  # duplicate
        item_to_record(make_item("a"), "amazon", STAC_URL, INGESTED_AT),  # same item, other AOI: kept
        item_to_record(make_item("b", drop=["eo:cloud_cover"]), "sahel", STAC_URL, INGESTED_AT),
    ]
    df = records_to_dataframe(records)

    assert list(df.columns) == COLUMNS
    assert len(df) == 3
    assert sorted(zip(df["item_id"], df["region"])) == [("a", "amazon"), ("a", "sahel"), ("b", "sahel")]
    assert df["cloud_cover"].dtype == "float64"
    assert df["cloud_cover"].isna().sum() == 1
    assert str(df["relative_orbit"].dtype) == "Int64"
    assert isinstance(df["acquired_at"].dtype, pd.DatetimeTZDtype)


def test_records_to_dataframe_empty():
    df = records_to_dataframe([])
    assert df.empty
    assert list(df.columns) == COLUMNS
