from datetime import date

import pytest

from ingestion.config import AOI, load_aois
from ingestion.stac_client import (
    RETRY_STATUS_CODES,
    build_retry,
    build_search_params,
    month_windows,
    search_items,
)


def test_month_windows_full_months():
    assert month_windows(date(2026, 1, 1), date(2026, 3, 31)) == [
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 31)),
    ]


def test_month_windows_partial_and_year_boundary():
    assert month_windows(date(2025, 12, 15), date(2026, 1, 10)) == [
        (date(2025, 12, 15), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 10)),
    ]


def test_month_windows_single_day():
    assert month_windows(date(2026, 5, 5), date(2026, 5, 5)) == [(date(2026, 5, 5), date(2026, 5, 5))]


def test_month_windows_rejects_inverted_range():
    with pytest.raises(ValueError):
        month_windows(date(2026, 2, 1), date(2026, 1, 1))


def test_build_search_params():
    params = build_search_params(
        (1.5, 13.0, 2.5, 14.0), (date(2026, 8, 1), date(2026, 8, 31)), "sentinel-2-l2a", max_items=10
    )
    assert params == {
        "collections": ["sentinel-2-l2a"],
        "bbox": [1.5, 13.0, 2.5, 14.0],
        "datetime": "2026-08-01T00:00:00Z/2026-08-31T23:59:59Z",
        "limit": 100,
        "max_items": 10,
    }


def test_retry_covers_rate_limits_and_post():
    retry = build_retry()
    assert retry.total == 5
    assert 429 in retry.status_forcelist
    assert set(RETRY_STATUS_CODES) <= set(retry.status_forcelist)
    assert retry.allowed_methods is None


def test_search_items_queries_each_month(make_item, fake_client_cls):
    items = [make_item("a", "2026-07-10T10:00:00Z"), make_item("b", "2026-08-20T10:00:00Z")]
    client = fake_client_cls(items)
    aoi = AOI("sahel", (1.5, 13.0, 2.5, 14.0))

    found = list(search_items(client, aoi, date(2026, 7, 1), date(2026, 8, 31), "sentinel-2-l2a"))

    assert [i.id for i in found] == ["a", "b"]
    assert len(client.calls) == 2


def test_repo_aoi_config_is_valid():
    aois = load_aois()
    assert len(aois) == 6
    assert len({a.name for a in aois}) == len(aois)
    for a in aois:
        minx, miny, maxx, maxy = a.bbox
        assert -180 <= minx < maxx <= 180
        assert -90 <= miny < maxy <= 90
