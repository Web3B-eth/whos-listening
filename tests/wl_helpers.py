"""Load the plugin folder the way Hermes does. All data in tests is made up."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_plugin():
    """Import the repo root as a package, like Hermes' plugin loader.
    Skips when Hermes itself is not importable (run with the Hermes venv for these)."""
    pytest.importorskip("agent.memory_provider")
    name = "wl_plugin_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
