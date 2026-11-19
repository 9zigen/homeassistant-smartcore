"""Shared test config.

The hub tests talk to real localhost aiohttp test servers. When the suite runs
with pytest-homeassistant-custom-component installed (needed for
test_config_flow.py), its pytest-socket integration blocks socket creation for
every test; pull in pytest-socket's ``socket_enabled`` fixture for the
non-Home-Assistant tests so both suites pass in one run. Without those plugins
installed this conftest is a no-op.
"""

import pytest


@pytest.fixture(autouse=True)
def _sockets_for_hub_tests(request):
    if "test_config_flow" not in request.node.nodeid:
        try:
            request.getfixturevalue("socket_enabled")
        except pytest.FixtureLookupError:
            pass  # pytest-socket not installed: sockets are allowed anyway
    yield
