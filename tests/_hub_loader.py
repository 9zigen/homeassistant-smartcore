"""Load custom_components/atios/hub.py with its relative imports, without the
package __init__ (which needs Home Assistant). Shared by the hub tests."""

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "atios"


def load_hub():
    if "atios_pkg.hub" in sys.modules:
        return sys.modules["atios_pkg.hub"]
    package = types.ModuleType("atios_pkg")
    package.__path__ = [str(_ROOT)]
    sys.modules["atios_pkg"] = package
    spec = importlib.util.spec_from_file_location("atios_pkg.hub", _ROOT / "hub.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["atios_pkg.hub"] = module
    spec.loader.exec_module(module)
    return module
