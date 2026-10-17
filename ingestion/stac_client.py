"""Thin wrapper around pystac-client for chunked, retry-safe metadata searches."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, timedelta

import pystac
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from urllib3.util.retry import Retry

from ingestion.config import AOI

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


def build_retry() -> Retry:
    return Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=RETRY_STATUS_CODES,
        allowed_methods=None,  # retry POST searches too
    )


def open_client(url: str) -> Client:
    """Open the STAC API. Metadata only, so no Planetary Computer SAS signing is needed."""
    stac_io = StacApiIO(max_retries=build_retry(), timeout=60)
    return Client.open(url, stac_io=stac_io)


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split the inclusive range [start, end] into inclusive calendar-month chunks."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")

    windows = []
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1)
        window_end = min(next_month - timedelta(days=1), end)
        windows.append((cursor, window_end))
        cursor = next_month
    return windows


def format_datetime_range(window: tuple[date, date]) -> str:
    start, end = window
    return f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"


def build_search_params(
    bbox: tuple[float, float, float, float],
    window: tuple[date, date],
    collection: str,
    max_items: int | None = None,
) -> dict:
    return {
        "collections": [collection],
        "bbox": list(bbox),
        "datetime": format_datetime_range(window),
        "limit": PAGE_SIZE,
        "max_items": max_items,
    }


def search_items(
    client: Client,
    aoi: AOI,
    start: date,
    end: date,
    collection: str,
    max_items_per_window: int | None = None,
) -> Iterator[pystac.Item]:
    """Yield every item intersecting the AOI between start and end, one month at a time."""
    for window in month_windows(start, end):
        params = build_search_params(aoi.bbox, window, collection, max_items_per_window)
        count = 0
        for item in client.search(**params).items():
            count += 1
            yield item
        logger.info("%s %s..%s: %d items", aoi.name, window[0], window[1], count)
