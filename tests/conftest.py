import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("service", "tools", "traffic"):
    sys.path.insert(0, str(ROOT / sub))

os.environ.setdefault("RATES_FILE", str(ROOT / "service" / "rates.yaml"))


def load_module(name: str, path: Path):
    """Import a file under a unique module name (rail/app.py would clash with service/app)."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def rail_module():
    return load_module("pob_rail_app", ROOT / "rail" / "app.py")
