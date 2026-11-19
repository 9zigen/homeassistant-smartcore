"""Connection hub for a single Atios SmartCore.

Transport is native aiohttp — no external DALI library. The SmartCore exposes:

  * a confirmed HTTP endpoint ``POST /api/dali/iface`` that sends a raw DALI
    frame and (with ``wait_response``) returns the answer as
    ``{"success":true,"bus_busy":false,"collision_detected":false,"data":<byte>}``;
  * the DALI IP Interface websocket at ``ws://<host>/`` that streams
    ``daliMonitor`` frames (bus traffic, incl. DALI-2 input/button events).
    Confirmed on fw 2.7.9: on connect it greets with ``{"type":"info"}``
    reporting name "dali-iot", protocolVersion 3.0.

DALI frames go over the DALI IP Interface websocket as ``daliFrame`` while it is
connected and greeted us (queries are answered with ``daliAnswer``, confirmed on
3.2.18), and over the HTTP endpoint otherwise, so lights keep working without
the socket. Every bus frame on the socket, our own included, is handed to the
bus listeners, which keep the light state current (gear_state.py).

What the firmware requires for that stream (checked against the SmartCore
firmware source, lib-dali ``dali_transport_layer.c`` / ``dali_ip_websocket.c``):

  * ``daliMonitor`` JSON is sent only while the device setting
    "DALI IP Interface" (System → DALI in the web UI, ``dali_ip_interface`` in
    ``GET /settings``) is on. It is off by default. While it is off the socket
    still connects and greets, but no bus frame ever arrives. We read the flag
    on setup and on every connect, and report it via add_ip_interface_listener.
  * No subscribe message is needed. ``/cmd/dali_monitor_start`` only drives the
    web UI's plain-text ``DALI:...`` monitor on ``/ws`` (broadcast to every ws
    client, so it also shows up here); it has no effect on ``daliMonitor``.
    A text "ping" is not understood on ``/`` either — the device just logs a
    JSON parse error — so keepalive is left to the ws-level heartbeat.
  * The socket serves ONE client: the firmware keeps a single global fd and the
    most recent handshake takes the stream over. Another DALI IP Interface client on
    ``/`` (tools/monitor.py, a second HA, DALI Cockpit) silences us without
    closing our socket, and we stay silent after it leaves until we reconnect.
  * Every client is greeted with ``{"type":"info"}`` right after the
    handshake. Firmware built on ESP-IDF v5.5.5/v6.0.1+ (seen on 3.2.2) never runs the
    handler's handshake branch, so it registers no client and silently drops
    the greeting and everything after it. We watch for any DALI IP Interface JSON after
    connecting and report its absence via add_stream_listener.

A second socket, the web UI's ``ws://<host>/ws``, carries the input monitor of
newer firmware (see inputs.py). Only its built-in input edges are used, reported
through add_builtin_input_listener: the DALI IP Interface socket stays the one transport
for DALI, so the input monitor's DALI-2 events are ignored. Firmware without the
input monitor ignores the lease command and sends nothing on this socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from .const import (
    DALI_IP_ANSWER_TIMEOUT_S,
    DALI_IP_ECHO_CHECK_S,
    DALI_IP_ECHO_TIMEOUT_S,
    DALI_IP_GREETING_TIMEOUT_S,
    DALI_IP_SILENT_ANSWERS,
    RECONNECT_MAX,
    RECONNECT_MIN,
    UNAVAILABLE_AFTER_FAILURES,
)
from .dali import QUIESCENT_MODE_TIMEOUT_S, Frame, decode_quiescent_command
from .inputs import CMD_INPUT_MONITOR_START, LEASE_RENEW_S, parse_builtin_input

_LOGGER = logging.getLogger(__name__)

# Message types the DALI IP Interface socket sends. The web UI's own JSON notifications
# (ota_status, system_event, ...) are broadcast to every ws client, this one
# included, but carry no "type", so they don't count as the device talking.
_DALI_IP_TYPES = frozenset({"info", "daliMonitor", "daliFrame", "daliAnswer"})


@dataclass
class MonitorFrame:
    """A raw frame observed on the bus (decoded further by dali.decode_input_event)."""

    data: list[int]
    bits: int
    line: int | None = None
    framing_error: bool = False
    # Device clock (seconds since boot) when the frame was received, and the
    # socket's message counter (consecutive per connection, a gap means lost messages)
    device_timestamp: float | None = None
    counter: int | None = None
    # False for the SmartCore's own output (our frames, its own commands)
    external: bool = True


def _parse_ws_json(raw: str) -> dict | None:
    """Decode one ws text message as a JSON object, or None if it isn't one.

    Besides DALI IP Interface JSON the socket carries the web UI's plain-text
    bus monitor (``DALI:[IN],DA24 Evt,008001,NA``) whenever someone has started
    the DALI Monitor in the web UI. Those lines repeat frames we already get as
    daliMonitor JSON, so they are dropped here — parsing both would deliver
    every button press twice and break gesture synthesis.
    """
    if not raw or not raw.lstrip().startswith("{"):
        return None
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) else None


def _monitor_frame(msg: dict, *, own: bool = False) -> MonitorFrame | None:
    """Extract an inbound MonitorFrame from a daliMonitor message.

    Confirmed on fw 2.7.9, the SmartCore pushes each observed bus frame as::

        {"type":"daliMonitor","data":{"line":0,"bits":24,"externalSource":true,
         "data":[0,128,1], ...}, "timeSignature":{...}}

    ``externalSource`` is true for traffic that originated on the bus (input
    devices, gear answers) and false for the SmartCore's own output (our
    commands, and the arc-power frames it sends itself when a coupler is bound
    to a light). Only external frames are input-event candidates; ``own``
    returns the SmartCore's own frames as well.
    """
    if msg.get("type") != "daliMonitor":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, list) or not payload:
        return None
    external = bool(data.get("externalSource"))
    if not external and not own:
        return None  # our own outgoing frame, not something on the bus
    try:
        octets = [int(b) & 0xFF for b in payload]
    except (TypeError, ValueError):
        return None
    bits = data.get("bits")
    timestamp = data.get("timestamp")
    signature = msg.get("timeSignature")
    counter = signature.get("counter") if isinstance(signature, dict) else None
    return MonitorFrame(
        data=octets,
        bits=int(bits) if isinstance(bits, int) else len(octets) * 8,
        line=data.get("line"),
        framing_error=bool(data.get("framingError")),
        device_timestamp=float(timestamp) if isinstance(timestamp, (int, float)) else None,
        counter=counter if isinstance(counter, int) else None,
        external=external,
    )


def _notify(cbs: list[Callable[[bool], None]], value: bool) -> None:
    for cb in list(cbs):
        try:
            cb(value)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Atios state listener failed")


class AtiosHub:
    """Manage HTTP control and the (optional) monitor websocket for one SmartCore."""

    def __init__(self, host: str, line: int, session: aiohttp.ClientSession) -> None:
        self._base_url = host if host.startswith("http") else f"http://{host}"
        self._host = self._base_url.removeprefix("http://").removeprefix("https://")
        # The DALI IP Interface websocket lives at the root path (confirmed fw 2.7.9 and
        # firmware source). /ws also accepts a ws, but that is the web UI's
        # socket and only carries the plain-text monitor, never daliMonitor.
        self._ws_url = f"ws://{self._host}/"
        # The web UI socket, which carries the input monitor (see inputs.py)
        self._input_ws_url = f"ws://{self._host}/ws"
        self._line = line
        self._session = session

        self._task: asyncio.Task | None = None
        self._input_task: asyncio.Task | None = None
        self._input_ws_warned = False
        self._lease_warned = False
        self._builtin_input_cbs: list[Callable[[int, int], None]] = []
        # One DALI request at a time: the device answers /api/dali/iface in its
        # single web server task, and a query blocks that task until the bus is
        # idle and the answer is in. Parallel requests (all lights polled at
        # once, a scene) only queue up there and run into the HTTP timeout.
        self._dali_lock = asyncio.Lock()
        self._closing = False
        self._ws_warned = False
        self._monitor_cbs: list[Callable[[MonitorFrame], None]] = []
        # Every frame on the bus, ours included (light state mirror)
        self._bus_cbs: list[Callable[[MonitorFrame], None]] = []
        # Called on every connection the device greets, to read the state again
        self._connected_cbs: list[Callable[[], None]] = []
        # The DALI IP Interface socket, while connected
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        # Query waiting for its daliAnswer: the query bytes and the future for the answer
        self._pending_answer: tuple[list[int], asyncio.Future] | None = None
        # Frames that got no reply on the socket in a row, see DALI_IP_SILENT_ANSWERS
        self._ws_silent = 0
        self._ip_interface: bool | None = None
        self._ip_interface_cbs: list[Callable[[bool], None]] = []
        self._greeted = False  # DALI IP Interface JSON seen on the current connection
        self._stream_ok: bool | None = None
        self._stream_cbs: list[Callable[[bool], None]] = []
        # Stream that died after the greeting, see const.DALI_IP_ECHO_TIMEOUT_S
        self._echo_pending_since: float | None = None
        self._stream_lost = False
        self._stream_lost_cbs: list[Callable[[bool], None]] = []
        self._monitor_backoff = RECONNECT_MIN
        # Consecutive DALI requests that did not reach the device
        self._send_failures = 0
        self._available_cbs: list[Callable[[bool], None]] = []
        # Bus scan: quiescent mode seen on the bus, DALI requests paused meanwhile
        self._bus_scan = False
        self._bus_scan_cbs: list[Callable[[bool], None]] = []
        self._bus_scan_expiry: asyncio.TimerHandle | None = None
        self._info: dict = {}
        self.control_devices: list = []  # nvram.ControlDevice, set at setup
        self.input_devices: list = []  # nvram.InputDevice, set at setup
        self.nvm_loaded = False  # the NVRAM device model was read at setup

    @property
    def host(self) -> str:
        return self._host

    @property
    def serial(self) -> str | None:
        return self._info.get("serial")

    @property
    def sw_version(self) -> str | None:
        return self._info.get("version_string")

    @property
    def info(self) -> dict:
        return self._info

    @property
    def latest_version(self) -> str | None:
        """Available firmware version, from the /ota_status 'latest' block.

        Populated after async_ota_check(); None until a check has run.
        """
        latest = self._info.get("latest")
        if isinstance(latest, dict):
            return latest.get("version_string")
        return None

    @property
    def ip_interface_enabled(self) -> bool | None:
        """The device's "DALI IP Interface" setting; None until it is known."""
        return self._ip_interface

    async def async_fetch_settings(self) -> None:
        """Read the "DALI IP Interface" flag from GET /settings.

        The daliMonitor stream only flows while it is on (see module
        docstring). Unauthenticated, like the web UI's own settings read.
        Best-effort: on failure the last known state is kept.
        """
        try:
            async with self._session.get(
                f"{self._base_url}/settings", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: settings fetch failed: %s", self._host, err)
            return
        if isinstance(body, dict) and isinstance(body.get("dali_ip_interface"), bool):
            self._set_ip_interface(body["dali_ip_interface"])

    def _set_ip_interface(self, enabled: bool) -> None:
        if enabled == self._ip_interface:
            return
        self._ip_interface = enabled
        _notify(self._ip_interface_cbs, enabled)

    @property
    def stream_ok(self) -> bool | None:
        """Whether the DALI IP Interface socket talks to us; None until it is known."""
        return self._stream_ok

    def _set_stream_ok(self, ok: bool) -> None:
        if ok == self._stream_ok:
            return
        self._stream_ok = ok
        _notify(self._stream_cbs, ok)

    async def async_fetch_info(self) -> dict:
        """Read GET /ota_status (serial, firmware version, update flag)."""
        try:
            async with self._session.get(
                f"{self._base_url}/ota_status", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                body = await resp.json(content_type=None)
                if isinstance(body, dict):
                    self._info = body
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: ota_status fetch failed: %s", self._host, err)
        return self._info

    async def async_fetch_nvm_section(self, section: str) -> list[dict] | None:
        """Read one NVRAM section via GET /api/dali/nvm, following pagination.

        The web configurator pages with limit=4; we mirror that exactly since
        it is the only request shape confirmed against the firmware. Returns
        None when the endpoint is unreachable (older firmware), so callers can
        fall back.
        """
        devices: list[dict] = []
        offset = 0
        for _ in range(32):  # 32 * 4 = 128 > max 64 addresses + groups
            url = (
                f"{self._base_url}/api/dali/nvm"
                f"?section={section}&offset={offset}&limit=4"
            )
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Atios %s: nvm fetch %s failed: %s", self._host, section, err
                )
                return None if not devices else devices
            if not isinstance(body, dict) or not body.get("success"):
                return None if not devices else devices
            devices.extend((body.get("data") or {}).get(section) or [])
            pagination = body.get("pagination") or {}
            if not pagination.get("has_more"):
                break
            offset += pagination.get("limit", 4)
        return devices

    async def async_trigger_ota(self) -> bool:
        """Start a firmware update via POST /cmd/ota_update.

        Confirmed by Atios. Note: only works if the device has no web password.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/ota_update", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Atios %s: OTA trigger failed: %s", self._host, err)
            return False

    async def async_ota_check(self) -> None:
        """Ask the device to check for updates (POST /cmd/ota_update_check).

        Afterwards /ota_status carries a 'latest' block with the available
        version. Best-effort: firmware without the endpoint just 404s.
        """
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/ota_update_check",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                resp.raise_for_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: ota_update_check failed: %s", self._host, err)

    # ---- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run_monitor(), name=f"atios-{self._host}")
        self._input_task = asyncio.create_task(
            self._run_input_monitor(), name=f"atios-inputs-{self._host}"
        )

    async def async_stop(self) -> None:
        # The input monitor lease is not released: the web UI may hold it too,
        # and the firmware ends it by itself once nobody renews it.
        self._closing = True
        if self._bus_scan_expiry is not None:
            self._bus_scan_expiry.cancel()
            self._bus_scan_expiry = None
        for task in (self._task, self._input_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def add_monitor_listener(self, cb: Callable[[MonitorFrame], None]) -> Callable[[], None]:
        self._monitor_cbs.append(cb)
        return lambda: self._monitor_cbs.remove(cb)

    def add_bus_listener(self, cb: Callable[[MonitorFrame], None]) -> Callable[[], None]:
        """Call ``cb(frame)`` for every frame on the bus, the SmartCore's own included.

        Without a live daliMonitor stream, frames we send are handed over right
        after sending, so the light state still follows our own commands.
        """
        self._bus_cbs.append(cb)
        return lambda: self._bus_cbs.remove(cb)

    def add_connected_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Call ``cb()`` each time the DALI IP Interface socket (re)connects and is greeted."""
        self._connected_cbs.append(cb)
        return lambda: self._connected_cbs.remove(cb)

    @property
    def bus_stream_live(self) -> bool:
        """daliMonitor frames are arriving: all bus traffic, ours included, is seen."""
        return (
            self._ws is not None
            and self._greeted
            and self._ip_interface is True
            and not self._stream_lost
        )

    @property
    def dali_ip_connected(self) -> bool:
        """The DALI IP Interface socket is connected and the device greeted us on it."""
        return self._ws is not None and self._greeted

    @property
    def _ws_usable(self) -> bool:
        return (
            self._ws is not None
            and not self._ws.closed
            and self._greeted
            and self._ws_silent < DALI_IP_SILENT_ANSWERS
        )

    def add_builtin_input_listener(self, cb: Callable[[int, int], None]) -> Callable[[], None]:
        """Call ``cb(pin, level)`` on every built-in input edge (pin 0 = input 1, raw level)."""
        self._builtin_input_cbs.append(cb)
        return lambda: self._builtin_input_cbs.remove(cb)

    def add_ip_interface_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(enabled)`` whenever the "DALI IP Interface" state changes."""
        self._ip_interface_cbs.append(cb)
        return lambda: self._ip_interface_cbs.remove(cb)

    @property
    def available(self) -> bool:
        """False once UNAVAILABLE_AFTER_FAILURES DALI requests in a row failed to reach it."""
        return self._send_failures < UNAVAILABLE_AFTER_FAILURES

    def add_available_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(available)`` when the device stops or starts answering DALI requests."""
        self._available_cbs.append(cb)
        return lambda: self._available_cbs.remove(cb)

    def _record_send(self, reached: bool) -> None:
        was_available = self.available
        self._send_failures = 0 if reached else self._send_failures + 1
        if self.available != was_available:
            _notify(self._available_cbs, self.available)

    @property
    def bus_scan_active(self) -> bool:
        """True while a bus scan holds the DALI-2 devices in quiescent mode.

        Seen from the START / STOP QUIESCENT MODE broadcast on the DALI IP
        Interface stream (the SmartCore's own frames included), so it needs that
        setting on. DALI requests from Home Assistant are paused meanwhile, so
        they do not disturb addressing and do not read half-commissioned gear.
        """
        return self._bus_scan

    def add_bus_scan_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(active)`` when a bus scan starts or ends."""
        self._bus_scan_cbs.append(cb)
        return lambda: self._bus_scan_cbs.remove(cb)

    def _set_bus_scan(self, active: bool) -> None:
        if self._bus_scan_expiry is not None:
            self._bus_scan_expiry.cancel()
            self._bus_scan_expiry = None
        if active:
            # Devices leave quiescent mode on their own 15 min after the last START,
            # so a missed STOP (stream lost, web UI closed mid-scan) cannot pause us for good.
            self._bus_scan_expiry = asyncio.get_running_loop().call_later(
                QUIESCENT_MODE_TIMEOUT_S, self._set_bus_scan, False
            )
        if active == self._bus_scan:
            return
        self._bus_scan = active
        _LOGGER.info(
            "Atios %s: DALI bus scan %s, DALI requests %s",
            self._host,
            "started" if active else "ended",
            "paused" if active else "resumed",
        )
        _notify(self._bus_scan_cbs, active)

    @property
    def stream_lost(self) -> bool:
        """True while the DALI IP Interface stream is considered dead after it had greeted us."""
        return self._stream_lost

    def add_stream_lost_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(lost)`` when the DALI IP Interface stream dies or recovers."""
        self._stream_lost_cbs.append(cb)
        return lambda: self._stream_lost_cbs.remove(cb)

    def _set_stream_lost(self, lost: bool) -> None:
        if lost == self._stream_lost:
            return
        self._stream_lost = lost
        _notify(self._stream_lost_cbs, lost)

    def add_stream_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``cb(ok)`` when the DALI IP Interface socket starts or stops talking to us."""
        self._stream_cbs.append(cb)
        return lambda: self._stream_cbs.remove(cb)

    # ---- monitor websocket (receive-only, best-effort) --------------------

    async def _run_monitor(self) -> None:
        self._monitor_backoff = RECONNECT_MIN
        while not self._closing:
            watchdog: asyncio.Task | None = None
            echo_watchdog: asyncio.Task | None = None
            try:
                # heartbeat: ws-level PING, answered by the device's HTTP
                # server itself. Nothing else needs sending: the daliMonitor
                # stream needs no subscribe (see module docstring).
                async with self._session.ws_connect(
                    self._ws_url, heartbeat=30, timeout=aiohttp.ClientTimeout(total=10)
                ) as ws:
                    self._ws_warned = False
                    self._ws = ws
                    self._ws_silent = 0
                    _LOGGER.info("Atios %s: monitor websocket connected", self._host)
                    self._greeted = False
                    self._echo_pending_since = None
                    watchdog = asyncio.create_task(self._greeting_watchdog())
                    echo_watchdog = asyncio.create_task(self._echo_watchdog(ws))
                    # Re-check on every connect, so a setting changed while we
                    # were away is picked up.
                    await self.async_fetch_settings()
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_ws_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self._closing:
                    break
                # Quiet: lights work over HTTP regardless; warn once, then debug.
                if not self._ws_warned:
                    _LOGGER.info(
                        "Atios %s: monitor websocket unavailable (%s). Lights work "
                        "over HTTP; button events resume once it reconnects. "
                        "Retrying quietly.",
                        self._host,
                        err,
                    )
                    self._ws_warned = True
                else:
                    _LOGGER.debug("Atios %s: ws retry (%s)", self._host, err)
            finally:
                self._ws = None
                self._greeted = False
                self._fail_pending_answer()
                for task in (watchdog, echo_watchdog):
                    if task is not None:
                        task.cancel()
            if not self._closing:
                # Reset to RECONNECT_MIN once bus frames flow (_handle_ws_text), so
                # a socket that greets but stays dead is retried ever more slowly.
                await asyncio.sleep(self._monitor_backoff)
                self._monitor_backoff = min(RECONNECT_MAX, self._monitor_backoff * 2)

    async def _echo_watchdog(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Reconnect when a query was answered but its echo never arrived."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(DALI_IP_ECHO_CHECK_S)
            pending = self._echo_pending_since
            if (
                pending is None
                or self._ip_interface is not True
                or loop.time() - pending < DALI_IP_ECHO_TIMEOUT_S
            ):
                continue
            _LOGGER.warning(
                "Atios %s: the DALI IP websocket stopped streaming bus frames, reconnecting",
                self._host,
            )
            self._set_stream_lost(True)
            self._echo_pending_since = None
            await ws.close()
            return

    async def _greeting_watchdog(self) -> None:
        """Flag a connection on which the device never says anything."""
        await asyncio.sleep(DALI_IP_GREETING_TIMEOUT_S)
        if not self._greeted:
            self._set_stream_ok(False)

    def _handle_ws_text(self, raw: str) -> None:
        """Parse a ws text message and dispatch monitor frames.

        Only DALI IP Interface JSON is used (see _parse_ws_json); the web UI's
        ``DALI:...`` text mirror of the same frames is ignored so each bus
        event is delivered exactly once.
        """
        msg = _parse_ws_json(raw)
        if msg is None or msg.get("type") not in _DALI_IP_TYPES:
            return
        # Any DALI IP Interface message (the "info" greeting first) shows the device has
        # registered us as its client and will stream to us.
        first = not self._greeted
        self._greeted = True
        self._set_stream_ok(True)
        if first:
            for cb in list(self._connected_cbs):
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Atios connected listener failed")
        if msg.get("type") == "daliAnswer":
            self._resolve_answer(msg.get("data"))
            return
        if msg.get("type") != "daliMonitor":
            return
        # The device sends no daliMonitor at all while "DALI IP Interface" is
        # off, so any one of them (ours included) proves it is on now.
        self._set_ip_interface(True)
        # ...and that the stream is alive, echoes of our own frames included
        self._echo_pending_since = None
        self._monitor_backoff = RECONNECT_MIN
        self._set_stream_lost(False)
        self._track_bus_scan(msg)
        frame = _monitor_frame(msg, own=True)
        if frame is None:
            return
        self._dispatch_bus(frame)
        if not frame.external:
            return
        for cb in list(self._monitor_cbs):
            try:
                cb(frame)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Atios monitor listener failed")

    def _dispatch_bus(self, frame: MonitorFrame) -> None:
        for cb in list(self._bus_cbs):
            try:
                cb(frame)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Atios bus listener failed")

    def _resolve_answer(self, data: object) -> None:
        """Hand a daliAnswer to the query waiting for it (matched by the query bytes)."""
        pending = self._pending_answer
        if pending is None or not isinstance(data, dict):
            return
        query, future = pending
        if data.get("queryData") != query or future.done():
            return
        answer = data.get("daliData")
        future.set_result([answer & 0xFF] if data.get("result") == 8 and isinstance(answer, int) else None)

    def _fail_pending_answer(self) -> None:
        if self._pending_answer is not None and not self._pending_answer[1].done():
            self._pending_answer[1].set_exception(ConnectionError("DALI IP Interface socket closed"))
        self._pending_answer = None

    def _track_bus_scan(self, msg: dict) -> None:
        """Follow quiescent mode from any daliMonitor frame, the SmartCore's own included."""
        data = msg.get("data")
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, list) or not isinstance(data.get("bits"), int):
            return
        try:
            octets = [int(b) & 0xFF for b in payload]
        except (TypeError, ValueError):
            return
        quiescent = decode_quiescent_command(octets, data["bits"])
        if quiescent is not None:
            self._set_bus_scan(quiescent)

    # ---- input monitor on the web UI socket (best-effort) -----------------

    async def _run_input_monitor(self) -> None:
        backoff = RECONNECT_MIN
        while not self._closing:
            lease: asyncio.Task | None = None
            try:
                async with self._session.ws_connect(
                    self._input_ws_url, heartbeat=30, timeout=aiohttp.ClientTimeout(total=10)
                ) as ws:
                    self._input_ws_warned = False
                    backoff = RECONNECT_MIN
                    _LOGGER.debug("Atios %s: input monitor websocket connected", self._host)
                    lease = asyncio.create_task(self._renew_input_lease())
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle_input_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self._closing:
                    break
                log = _LOGGER.debug if self._input_ws_warned else _LOGGER.info
                log("Atios %s: input monitor websocket unavailable (%s)", self._host, err)
                self._input_ws_warned = True
            finally:
                if lease is not None:
                    lease.cancel()
            if not self._closing:
                await asyncio.sleep(backoff)
                backoff = min(RECONNECT_MAX, backoff * 2)

    async def _renew_input_lease(self) -> None:
        """Hold the input monitor lease while the socket is connected."""
        while True:
            await self._post_input_lease()
            await asyncio.sleep(LEASE_RENEW_S)

    async def _post_input_lease(self) -> None:
        try:
            async with self._session.post(
                f"{self._base_url}/cmd/{CMD_INPUT_MONITOR_START}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 401 and not self._lease_warned:
                    self._lease_warned = True
                    _LOGGER.warning(
                        "Atios %s: the SmartCore has a web password, so it does not "
                        "send built-in input events to Home Assistant.",
                        self._host,
                    )
                resp.raise_for_status()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Atios %s: input monitor lease failed: %s", self._host, err)

    def _handle_input_text(self, raw: str) -> None:
        edge = parse_builtin_input(raw)
        if edge is None:
            return
        for cb in list(self._builtin_input_cbs):
            try:
                cb(edge.pin, edge.level)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Atios built-in input listener failed")

    # ---- sending / querying -------------------------------------------------

    async def send_frame(self, frame: Frame) -> list[int] | None:
        """Send a DALI frame, one request at a time.

        Over the DALI IP Interface socket while it is usable, else over HTTP.
        Returns the answer byte(s) as list[int] for QUERY frames, else None.
        """
        async with self._dali_lock:
            # Checked after waiting for the lock: a scan may have started meanwhile.
            # Quiescent mode frames still go out, so a stuck scan can be ended.
            if self._bus_scan and decode_quiescent_command(frame.data, frame.bits) is None:
                _LOGGER.debug("Atios %s: bus scan running, %s not sent", self._host, frame.data)
                return None
            stream_live = self.bus_stream_live
            if self._ws_usable:
                sent, answer = await self._send_frame_ws(frame)
                if sent:
                    self._after_send(frame, stream_live)
                    return answer
            answer = await self._send_frame_now(frame)
            if self._send_failures == 0:
                self._after_send(frame, stream_live)
            return answer

    def _after_send(self, frame: Frame, stream_live: bool) -> None:
        """Without a daliMonitor stream, our own commands are the only bus traffic we see."""
        if stream_live or frame.wait_for_answer:
            return
        self._dispatch_bus(MonitorFrame(data=list(frame.data), bits=frame.bits, external=False))

    async def _send_frame_ws(self, frame: Frame) -> tuple[bool, list[int] | None]:
        """Send a frame as daliFrame; (sent, answer). Not sent: use HTTP instead."""
        ws = self._ws
        if ws is None:
            return False, None
        message = {
            "type": "daliFrame",
            "data": {
                "line": self._line,
                "numberOfBits": frame.bits,
                "daliData": list(frame.data),
                "mode": {
                    "priority": frame.priority,
                    "sendTwice": frame.send_twice,
                    "waitForAnswer": frame.wait_for_answer,
                },
            },
        }
        future: asyncio.Future | None = None
        if frame.wait_for_answer:
            future = asyncio.get_running_loop().create_future()
            self._pending_answer = (list(frame.data), future)
        try:
            await ws.send_str(json.dumps(message))
            if future is None:
                # Commands get no reply of their own on the socket
                self._ws_silent = 0
                self._record_send(True)
                return True, None
            answer = await asyncio.wait_for(future, DALI_IP_ANSWER_TIMEOUT_S)
        except (asyncio.TimeoutError, aiohttp.ClientError, RuntimeError, ConnectionError):
            self._ws_silent += 1
            _LOGGER.debug(
                "Atios %s: no reply on the DALI IP Interface socket for %s, using HTTP",
                self._host,
                frame.data,
            )
            return False, None
        finally:
            if future is not None:
                self._pending_answer = None
        self._ws_silent = 0
        self._record_send(True)
        return True, answer

    async def _send_frame_now(self, frame: Frame) -> list[int] | None:
        payload = {
            "repeat_twice": frame.send_twice,
            "wait_response": frame.wait_for_answer,
            "bits": frame.bits,
            "data": list(frame.data),
        }
        url = f"{self._base_url}/api/dali/iface"
        reached = False
        try:
            async with self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                resp.raise_for_status()
                reached = True
                self._record_send(True)
                if not frame.wait_for_answer:
                    return None
                body = await resp.json(content_type=None)
                # {"success":true,"bus_busy":false,"collision_detected":false,"data":255}
                if not isinstance(body, dict):
                    return None
                if body.get("collision_detected"):
                    _LOGGER.debug("Atios %s: DALI collision on %s", self._host, frame.data)
                # Without an answer the firmware still reports "data": 0, which
                # would read as a real answer (level 0, light off).
                if not body.get("success"):
                    return None
                answer = body.get("data")
                if answer is None:
                    return None
                # An answered query was transmitted, so its echo must arrive
                # on the DALI IP Interface socket (see _echo_watchdog).
                if self._echo_pending_since is None:
                    self._echo_pending_since = asyncio.get_running_loop().time()
                if isinstance(answer, int):
                    return [answer]
                if isinstance(answer, list):
                    return answer
                return None
        except Exception as err:  # noqa: BLE001
            if reached:
                _LOGGER.debug("Atios %s: unexpected DALI answer: %s", self._host, err)
                return None
            # Log the start of an outage, not every request while it lasts
            log = _LOGGER.error if self._send_failures == 0 else _LOGGER.debug
            log("Atios %s: HTTP send failed: %s", self._host, err)
            self._record_send(False)
            return None

    async def async_test_connection(self) -> bool:
        """Config-flow reachability check: gear-present query over HTTP."""
        from .dali import OP_QUERY_CONTROL_GEAR_PRESENT, Target, query

        result = await self.send_frame(query(Target.short(0), OP_QUERY_CONTROL_GEAR_PRESENT))
        if result is not None:
            return True
        # gear may not answer; treat a live HTTP endpoint as reachable
        try:
            async with self._session.get(
                f"{self._base_url}/api/dali/iface",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status < 500
        except Exception:  # noqa: BLE001
            return False
