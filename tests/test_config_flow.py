"""Config flow tests (Bronze quality scale: full flow coverage).

Runs on pytest-homeassistant-custom-component; no hardware. The connection
check is mocked at AtiosHub.async_test_connection, everything else is the real
flow code.
"""

from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atios.const import DOMAIN

CONF_LINE = "line"

ZEROCONF_INFO = ZeroconfServiceInfo(
    ip_address=ip_address("192.168.1.154"),
    ip_addresses=[ip_address("192.168.1.154")],
    hostname="smartcore.local.",
    name="SmartCore A1B2._atios._tcp.local.",
    port=80,
    type="_atios._tcp.local.",
    properties={"sn": "SC-00A1B2", "model": "ADE-R12", "fw": "3.2.64"},
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let hass load custom_components/atios."""
    yield


def _patch_connection(ok: bool):
    return patch(
        "custom_components.atios.config_flow.AtiosHub.async_test_connection",
        AsyncMock(return_value=ok),
    )


def _patch_setup():
    return patch("custom_components.atios.async_setup_entry", AsyncMock(return_value=True))


async def test_user_flow_success(hass):
    """Valid host -> entry created with host + line."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}

    with _patch_connection(True), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.154", CONF_LINE: 0}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SmartCore (192.168.1.154)"
    assert result["data"] == {CONF_HOST: "192.168.1.154", CONF_LINE: 0}
    assert result["result"].unique_id == "192.168.1.154"


async def test_user_flow_strips_whitespace(hass):
    """Host is stripped before use."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with _patch_connection(True), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "  192.168.1.154  ", CONF_LINE: 1}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "SmartCore (192.168.1.154)"
    assert result["data"] == {CONF_HOST: "192.168.1.154", CONF_LINE: 1}


async def test_user_flow_cannot_connect_then_recover(hass):
    """No answer -> cannot_connect error; retry with success -> entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with _patch_connection(False):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.99", CONF_LINE: 0}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    with _patch_connection(True), _patch_setup():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.99", CONF_LINE: 0}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_invalid_line_rejected(hass):
    """Line outside 0..3 fails schema validation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with pytest.raises(Exception):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.154", CONF_LINE: 7}
        )


async def test_user_flow_duplicate_aborts(hass):
    """Adding the same host twice aborts with already_configured."""
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.154",
        data={CONF_HOST: "192.168.1.154", CONF_LINE: 0},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.1.154", CONF_LINE: 0}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_zeroconf_flow_success(hass):
    """mDNS discovery -> confirm form -> entry; unique id from the sn TXT."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZEROCONF_INFO,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {
        "name": "SmartCore A1B2._atios._tcp.local",
        "host": "192.168.1.154",
    }

    with _patch_connection(True), _patch_setup():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "192.168.1.154", CONF_LINE: 0}
    assert result["result"].unique_id == "SC00A1B2"


async def test_zeroconf_without_serial_uses_host(hass):
    """No usable TXT id -> host is the unique id."""
    info = ZeroconfServiceInfo(
        ip_address=ip_address("192.168.1.154"),
        ip_addresses=[ip_address("192.168.1.154")],
        hostname="smartcore.local.",
        name="SmartCore._atios._tcp.local.",
        port=80,
        type="_atios._tcp.local.",
        properties={},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=info
    )
    assert result["type"] is FlowResultType.FORM

    with _patch_connection(True), _patch_setup():
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["result"].unique_id == "192.168.1.154"


async def test_zeroconf_duplicate_aborts_and_updates_host(hass):
    """Known serial at a new IP -> abort, and the entry's host is updated."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="SC00A1B2",
        data={CONF_HOST: "192.168.1.20", CONF_LINE: 0},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZEROCONF_INFO,
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.1.154"


async def test_zeroconf_confirm_cannot_connect_aborts(hass):
    """Discovered device that stops answering -> abort cannot_connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=ZEROCONF_INFO,
    )
    with _patch_connection(False):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_options_flow_panel(hass):
    """Options flow stores panel settings; blank URL falls back to default."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.154",
        data={CONF_HOST: "192.168.1.154", CONF_LINE: 0},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "panel_enabled": True,
            "panel_url": "",
            "panel_title": "SmartCore",
            "panel_icon": "mdi:lightbulb-group",
            "panel_require_admin": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # blank URL was dropped -> runtime falls back to http://<host>/
    assert "panel_url" not in result["data"]
    assert result["data"]["panel_enabled"] is True
