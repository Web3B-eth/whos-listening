"""Run the shared JSON test vectors in tests/vectors/. All data is made up."""

import json
from pathlib import Path

import pytest

from whos_listening.recall import strip_private
from whos_listening.routes import Route, RouteSettings, classify
from whos_listening.store import Provenance

VECTORS = Path(__file__).parent / "vectors"


def load(name):
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", load("routes.json"), ids=lambda c: c["name"])
def test_route_vectors(case):
    route = Route(**case["route"]) if case["route"] is not None else None
    settings = RouteSettings.from_dict(case.get("settings") or {})
    assert classify(route, settings).tier == case["tier"]


@pytest.mark.parametrize("case", load("strip.json"), ids=lambda c: c["name"])
def test_strip_vectors(case):
    assert strip_private(case["text"]) == case["shared"]


@pytest.mark.parametrize("case", load("fingerprints.json"), ids=lambda c: f'{c["forgotten"]}|{c["new"]}')
def test_fingerprint_vectors(case, store):
    m = store.add(case["forgotten"], "shared", Provenance())
    store.forget(m.id)
    assert store.is_forgotten_text(case["new"]) is case["blocked"]
