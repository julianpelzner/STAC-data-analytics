import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pystac
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
STAC_URL = "https://example.test/stac/v1"
INGESTED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def item_dict() -> dict:
    return json.loads((FIXTURES / "sentinel2_l2a_item.json").read_text())


@pytest.fixture
def make_item(item_dict):
    """Build a pystac.Item from the fixture with optional overrides."""

    def _make(item_id=None, dt=None, properties=None, drop=()):
        d = copy.deepcopy(item_dict)
        if item_id:
            d["id"] = item_id
        if dt:
            d["properties"]["datetime"] = dt
        d["properties"].update(properties or {})
        for key in drop:
            d["properties"].pop(key, None)
        return pystac.Item.from_dict(d)

    return _make


class FakeSearch:
    def __init__(self, items):
        self._items = items

    def items(self):
        return iter(self._items)


class FakeClient:
    """Stands in for pystac_client.Client: returns items whose datetime falls in the requested window."""

    def __init__(self, items):
        self._items = items
        self.calls = []

    def search(self, **params):
        self.calls.append(params)
        start, end = (datetime.fromisoformat(s.replace("Z", "+00:00")) for s in params["datetime"].split("/"))
        return FakeSearch([i for i in self._items if start <= i.datetime <= end])


@pytest.fixture
def fake_client_cls():
    return FakeClient
