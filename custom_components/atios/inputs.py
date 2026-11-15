"""Built-in inputs from the input monitor of the SmartCore web UI socket (``ws://<host>/ws``).

Pure helpers, no Home Assistant or aiohttp imports, so they are unit tested
standalone (tests/test_inputs.py).

SmartCore firmware with the input monitor streams plain-text messages to every
``/ws`` client while a lease is held. ``POST /cmd/input_monitor_start`` starts
or renews the lease; without a renewal the feed stops on its own about 30 s
later, so a client that goes away cannot leave the device reporting. The
firmware keeps the command endpoint behind the web password, like
``/cmd/ota_update``.

  * ``IN:<pin>,<level>`` — a built-in input edge. ``pin`` is the input index
    (0 = input 1), ``level`` the raw electrical level 0/1 (the accessory invert
    of the SmartCore configuration is not applied). One message per physical
    edge, however many accessories use the input. There is no snapshot: an
    input is unknown until it changes for the first time.
  * ``DALI2:<address>,<instance>,<event_info>`` — DALI-2 instance events, for the
    web UI. Ignored here: the DALI IP Interface socket stays the one transport
    for DALI, and it carries every event scheme, not only Device/Instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The firmware lease runs 30 s, renew well before it ends
LEASE_RENEW_S = 10
CMD_INPUT_MONITOR_START = "input_monitor_start"

BUILTIN_INPUT_COUNT = 12

_BUILTIN_RE = re.compile(r"^IN:(\d{1,3}),([01])$")


@dataclass(frozen=True)
class BuiltinInputEdge:
    """A built-in input changed its raw level."""

    pin: int
    level: int


def parse_builtin_input(raw: str) -> BuiltinInputEdge | None:
    """Decode one ``/ws`` text message, None for anything that is not a built-in input edge.

    The socket also carries the web UI's JSON notifications, its DALI / KNX bus
    monitor lines and the input monitor's DALI-2 events, all ignored here.
    """
    if not isinstance(raw, str) or not (match := _BUILTIN_RE.match(raw)):
        return None
    pin, level = int(match[1]), int(match[2])
    return BuiltinInputEdge(pin, level) if pin < BUILTIN_INPUT_COUNT else None
