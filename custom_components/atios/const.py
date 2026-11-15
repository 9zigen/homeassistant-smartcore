"""Constants for the Atios SmartCore integration."""

from __future__ import annotations

DOMAIN = "atios"

# config entry keys
CONF_HOST = "host"
CONF_LINE = "line"

# options (panel)
CONF_PANEL = "panel_enabled"
CONF_PANEL_URL = "panel_url"
CONF_PANEL_TITLE = "panel_title"
CONF_PANEL_ICON = "panel_icon"
CONF_PANEL_ADMIN = "panel_require_admin"

# legacy option: list[int] of DALI short addresses to expose as lights.
# Superseded by the NVRAM device model; still read as a fallback when the
# nvm endpoint is unavailable (older firmware).
CONF_LIGHTS = "lights"

DEFAULT_LINE = 0
DEFAULT_PANEL = True
DEFAULT_PANEL_TITLE = "SmartCore"
DEFAULT_PANEL_ICON = "mdi:lightbulb-group"
DEFAULT_PANEL_ADMIN = True

# how a raw daliMonitor input event is surfaced on the HA event bus
EVENT_DALI = f"{DOMAIN}_dali_event"
# how a built-in input edge is surfaced on the HA event bus
EVENT_INPUT = f"{DOMAIN}_input_event"

# service names
SERVICE_SEND_FRAME = "send_dali_frame"
SERVICE_RECALL_SCENE = "recall_scene"

# WS reconnect backoff (seconds)
RECONNECT_MIN = 2
RECONNECT_MAX = 60

# Repair issue raised while the SmartCore's "DALI IP Interface" setting is off.
# The daliMonitor stream on ws://<host>/ (every button/sensor event) is only
# sent while that setting is on; it is off by default.
ISSUE_DALI_IP_DISABLED = "dali_ip_interface_disabled"

# The DALI IP Interface socket greets every client with {"type":"info"} right
# after the handshake, whatever the settings. No DALI IP Interface JSON within
# this many seconds means the device will not stream on this socket at all: SmartCore firmware
# built on ESP-IDF v5.5.5/v6.0.1+ (seen on 3.2.2) never registers websocket
# clients, so it drops every message it would send them.
DALI_IP_GREETING_TIMEOUT_S = 10
ISSUE_DALI_IP_SILENT = "dali_ip_socket_silent"

# The DALI IP Interface socket echoes every frame the SmartCore transmits as a
# daliMonitor frame (externalSource false) while "DALI IP Interface" is on. A query answered
# over HTTP was transmitted, so no daliMonitor at all within this many seconds
# means the stream died after the greeting (the socket was taken over by another
# client, or closed on the device side). The hub then reconnects.
DALI_IP_ECHO_TIMEOUT_S = 15
DALI_IP_ECHO_CHECK_S = 5
ISSUE_DALI_IP_LOST = "dali_ip_stream_lost"
# Keys of the same two repair issues in v1.0.0 / v1.1.0, deleted on setup so an
# issue raised under the old key does not stay behind after the update
LEGACY_ISSUE_KEYS = ("lunatone_socket_silent", "lunatone_stream_lost")

# Lights turn unavailable after this many DALI requests in a row failed to reach
# the SmartCore (connection refused, timeout, HTTP error), and back on the next success.
UNAVAILABLE_AFTER_FAILURES = 3

# Software gesture timing for Device/Instance-scheme couplers.
# Confirmed on real Atios button couplers (2026-08-27): the bus only ever
# carries raw button_pressed/button_released (event_info 0/1) — short_press/
# double_press/long_press_* codes never appear, even for long holds and
# rapid double-taps. So those gestures are synthesized in event.py from the
# press/release timing instead of being read off the wire.
LONG_PRESS_START_S = 0.4  # hold time before synthesizing long_press_start
LONG_PRESS_REPEAT_S = 0.4  # interval between long_press_repeat while held
DOUBLE_PRESS_GAP_S = 0.4  # max gap between a release and the next press to count as double_press
# A long press ends after this hold time even without its release: a lost
# button_released would otherwise repeat long_press_repeat until the next press.
LONG_PRESS_MAX_S = 10
# Speed encoders send no stop event: rotate_stop follows this long after the last step
ENCODER_SPEED_STOP_S = 1.0
