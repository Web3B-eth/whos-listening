"""Test helpers. All data here is made up."""

import sys

import pytest

from whos_listening import venice  # noqa: F401  (loads the module so it gets reset)
from whos_listening.store import Provenance, SQLiteStore

# A made-up stand-in for Venice's public model list, so no test reaches the network.
FAKE_VENICE = {"grok-4-5": "private", "glm-5.3-flash": "private", "m": "private", "cfg-model": "private",
               "claude-opus-5-5": "anonymized", "jev-latest": "anonymized", "clef-api": "private"}


def _reset_venice():
    # Every loaded copy: provider tests load the plugin a second time under another package name.
    for name, mod in list(sys.modules.items()):
        if (name == "whos_listening.venice" or name.endswith(".whos_listening.venice")) and mod is not None:
            mod._reset_for_tests(dict(FAKE_VENICE))       # marked fresh, so nothing refreshes


@pytest.fixture(autouse=True)
def fake_venice_list():
    _reset_venice()
    yield
    _reset_venice()


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(tmp_path / "memories.sqlite")
    yield s
    s.close()


@pytest.fixture
def prov():
    return Provenance(written_by="user", via="test")
