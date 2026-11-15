"""Unit tests for the built-in input helpers (inputs.py).

Run: pytest tests/  (or: python3 tests/test_inputs.py)
"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "atios_inputs", Path(__file__).resolve().parent.parent / "custom_components" / "atios" / "inputs.py"
)
inputs = importlib.util.module_from_spec(_spec)
sys.modules["atios_inputs"] = inputs
_spec.loader.exec_module(inputs)


def test_parses_builtin_input_edges():
    assert inputs.parse_builtin_input("IN:0,1") == inputs.BuiltinInputEdge(0, 1)
    assert inputs.parse_builtin_input("IN:11,0") == inputs.BuiltinInputEdge(11, 0)


def test_ignores_everything_else_on_the_web_ui_socket():
    for raw in [
        "",
        "pong",
        '{"system_event":true}',
        "DALI:[IN],DA24 Evt,88841F,NA",
        "DALI2:1,0,1",  # DALI goes over the DALI IP Interface socket
        "TP:to bus,1.1.1,1/1/1,GroupValueWrite,01",
        "IN:12,1",  # 12 built-in inputs: 0..11
        "IN:3,2",
        "IN:3",
        "IN:-1,1",
        "IN:3,1,0",
        None,
    ]:
        assert inputs.parse_builtin_input(raw) is None, raw


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
