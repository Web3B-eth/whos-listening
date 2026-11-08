"""Test helpers. All data here is made up."""

import pytest

from whos_listening.store import Provenance, SQLiteStore

@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "memories.sqlite")
    yield s
    s.close()


@pytest.fixture
def prov():
    return Provenance(written_by="user", via="test")
