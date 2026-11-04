import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pystac
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
STAC_URL = "https://example.test/stac/v1"
INGESTED_AT = datetime(2026, 10, 1, tzinfo=UTC)


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
    """Stands in for pystac_client.Client.

    Returns items whose datetime falls in the requested window.
    """

    def __init__(self, items):
        self._items = items
        self.calls = []

    def search(self, **params):
        self.calls.append(params)
        start, end = (
            datetime.fromisoformat(s.replace("Z", "+00:00")) for s in params["datetime"].split("/")
        )
        return FakeSearch([i for i in self._items if start <= i.datetime <= end])


@pytest.fixture
def fake_client_cls():
    return FakeClient


# --------------------------------------------------------------------------- #
# Fakes for the BigQuery loader (no network, no GCP credentials).
# --------------------------------------------------------------------------- #


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.uploaded_from = None

    def upload_from_filename(self, filename):
        self.uploaded_from = filename
        self.bucket.client.uploads.append((self.bucket.name, self.name, filename))


class FakeBucket:
    def __init__(self, client, name):
        self.client = client
        self.name = name

    def blob(self, name):
        return FakeBlob(self, name)


class FakeStorageClient:
    """Records every (bucket, blob, local file) upload."""

    def __init__(self, **_):
        self.uploads = []

    def bucket(self, name):
        return FakeBucket(self, name)


class FakeLoadJob:
    def __init__(self, output_rows):
        self.output_rows = output_rows

    def result(self):
        return self


class FakeBQClient:
    """Stands in for bigquery.Client, recording every load job.

    By default a load reports the true row count of the uploaded local file (by
    resolving the gs:// URI through the paired `storage` fake), so the loader's
    post-load row-count check passes. `row_counts[table]` overrides that to
    force a mismatch.
    """

    def __init__(self, storage=None, row_counts=None, **_):
        self.storage = storage
        self.row_counts = row_counts or {}
        self.load_calls = []

    def load_table_from_uri(self, source_uri, destination, job_config=None, location=None):
        self.load_calls.append(
            {
                "uri": source_uri,
                "table": destination,
                "job_config": job_config,
                "location": location,
            }
        )
        table = destination.split(".")[-1]
        if table in self.row_counts:
            rows = self.row_counts[table]
        else:
            rows = _count_source_rows(self._local_for(source_uri))
        return FakeLoadJob(rows)

    def _local_for(self, source_uri):
        """Map gs://bucket/blob back to the local file the storage fake uploaded."""
        blob = source_uri.split("/", 3)[3]  # strip gs://bucket/
        for _, name, local in self.storage.uploads:
            if name == blob:
                return local
        raise AssertionError(f"no upload recorded for {source_uri}")


def _count_source_rows(path):
    import pandas as pd

    p = str(path)
    if p.endswith(".parquet"):
        return len(pd.read_parquet(p))
    return len(pd.read_csv(p))


@pytest.fixture
def fake_storage_cls():
    return FakeStorageClient


@pytest.fixture
def fake_bq_cls():
    return FakeBQClient
