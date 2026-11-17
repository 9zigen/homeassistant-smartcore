"""Parse the SmartCore NVRAM device model.

The SmartCore web configurator reads its device list from
``GET /api/dali/nvm?section=<control_devices|input_devices>&offset=N&limit=4``
(captured from the r12.atios.ch demo, firmware 2.7.x). This module turns those
payloads into the small dataclasses the entity platforms consume. Pure module,
no I/O, no Home Assistant imports — unit-tested against captured fixtures.

Instance types follow IEC 62386 parts: 1 = push button (-301), 2 = absolute
input (-302), 3 = occupancy sensor (-303), 4 = light sensor (-304),
6 = general purpose sensor.

``event_scheme`` uses the part-103 numbering: 0 = Instance, 1 = Device,
2 = Device/Instance, 3 = Device group, 4 = Instance group. Schemes 1 and 2 are
the ones our 24-bit event decoder resolves to a source (see dali.py); for the
others the entity keeps listening but cannot be matched upfront.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dali import ENCODER_MODELS, Target

INSTANCE_GENERIC = 0  # relative rotary encoder
INSTANCE_PUSHBUTTON = 1
INSTANCE_ABSOLUTE_INPUT = 2
INSTANCE_OCCUPANCY = 3
INSTANCE_LIGHT = 4
INSTANCE_GENERAL_PURPOSE = 6

SCHEME_DEVICE = 1
SCHEME_DEVICE_INSTANCE = 2

# group entries carry the DALI group number offset into the address field
GROUP_ADDRESS_BASE = 128


@dataclass(frozen=True)
class ControlDevice:
    """One configured control gear target (a short address or a group)."""

    name: str
    is_group: bool
    number: int  # short address 0..63 or group 0..15
    gear_groups: tuple[int, ...] = ()
    device_types: tuple[int, ...] = ()
    device_type_selected: int = 0
    min_level: int = 1
    max_level: int = 254
    # DT8 colour capabilities (QUERY COLOUR TYPE FEATURES, IEC 62386-209):
    # bit 0 = xy-coordinate capable, bit 1 = colour temperature Tc capable
    xy_capable: bool = False
    tc_capable: bool = False
    # Tc limits in mireds (physical warmest/coolest); None when not reported
    ct_warmest_mired: int | None = None
    ct_coolest_mired: int | None = None

    @property
    def target(self) -> Target:
        return Target.group(self.number) if self.is_group else Target.short(self.number)

    @property
    def supports_dt8(self) -> bool:
        return 8 in self.device_types or self.device_type_selected == 8


@dataclass(frozen=True)
class InputInstance:
    """One instance of a DALI-2 input device."""

    number: int
    type: int
    active: bool
    event_scheme: int
    resolution: int = 0
    # Stored by the SmartCore for absolute instances of models that send 0..255
    position_right_aligned: bool | None = None
    # General purpose sensor metadata (IEC 62386-306), read by the SmartCore at commissioning
    magnitude: int = 127
    input_signal_signed: bool = False
    quantity_name: int = 0
    unit_of_measure: int = 0

    def expected_signature(self, short_address: int) -> str | None:
        """The InputEvent.signature this instance's events decode to, if known.

        Device scheme carries the instance TYPE in the frame; Device/Instance
        scheme carries the instance NUMBER. Other schemes aren't resolvable
        upfront.
        """
        if self.event_scheme == SCHEME_DEVICE:
            return f"a{short_address}_t{self.type}_ix"
        if self.event_scheme == SCHEME_DEVICE_INSTANCE:
            return f"a{short_address}_tx_i{self.number}"
        return None


@dataclass(frozen=True)
class InputDevice:
    """One configured DALI-2 input device (24-bit control device)."""

    name: str
    address: int
    instances: tuple[InputInstance, ...] = field(default_factory=tuple)
    gtin: int | None = None

    def is_speed_encoder(self, instance: InputInstance) -> bool:
        """A relative instance that reports the turning speed instead of start / stop."""
        model = ENCODER_MODELS.get(self.gtin) if self.gtin is not None else None
        return model is not None and instance.number in model.speed_instances

    def sends_right_aligned_positions(self, instance: InputInstance) -> bool:
        """Absolute positions arrive as 0..255 instead of 10-bit events."""
        if instance.position_right_aligned is not None:
            return instance.position_right_aligned
        model = ENCODER_MODELS.get(self.gtin) if self.gtin is not None else None
        return model is not None and model.right_aligned_positions


def parse_control_devices(devices: list[dict]) -> list[ControlDevice]:
    """Parse the ``control_devices`` list; skips collision pseudo-entries."""
    out: list[ControlDevice] = []
    for d in devices:
        if d.get("collision"):
            continue
        addr = d.get("address")
        if addr is None:
            continue
        is_group = bool(d.get("is_group"))
        number = addr - GROUP_ADDRESS_BASE if is_group else addr
        if is_group and not 0 <= number <= 15:
            continue
        if not is_group and not 0 <= number <= 63:
            continue
        out.append(
            ControlDevice(
                name=d.get("name") or ("Group" if is_group else "Light") + f" {number}",
                is_group=is_group,
                number=number,
                gear_groups=tuple(d.get("gear_groups") or ()),
                device_types=tuple(d.get("device_types") or ()),
                device_type_selected=d.get("device_type_selected", 0),
                min_level=d.get("min_level", 1),
                max_level=d.get("max_level", 254),
                xy_capable=bool(_int(d.get("color_type_features"), 0) & 0b01),
                tc_capable=bool(_int(d.get("color_type_features"), 0) & 0b10),
                ct_warmest_mired=_mired(
                    d.get("color_temperature_physical_warmest")
                    or d.get("color_temperature_warmest")
                ),
                ct_coolest_mired=_mired(
                    d.get("color_temperature_physical_coolest")
                    or d.get("color_temperature_coolest")
                ),
            )
        )
    return out


def _mired(value: object) -> int | None:
    """A plausible Tc limit in mireds, else None (0/0xFFFF mean 'not reported')."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 0xFFFE:
        return value
    return None


def _parse_gtin(value: object) -> int | None:
    """The GTIN as one number; the NVRAM stores it as a list of bytes, most significant first."""
    if not isinstance(value, list) or not value:
        return None
    try:
        gtin = 0
        for byte in value:
            gtin = (gtin << 8) | (int(byte) & 0xFF)
    except (TypeError, ValueError):
        return None
    return gtin or None


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def parse_input_devices(devices: list[dict]) -> list[InputDevice]:
    """Parse the ``input_devices`` list."""
    out: list[InputDevice] = []
    for d in devices:
        addr = d.get("address")
        if addr is None or not 0 <= addr <= 63:
            continue
        instances = tuple(
            InputInstance(
                number=i.get("number", 0),
                type=i.get("type", 0),
                active=bool(i.get("active")),
                event_scheme=i.get("event_scheme", SCHEME_DEVICE_INSTANCE),
                resolution=i.get("resolution") or 0,
                position_right_aligned=(
                    i["position_right_aligned"]
                    if isinstance(i.get("position_right_aligned"), bool)
                    else None
                ),
                magnitude=_int(i.get("magnitude"), 127),
                # -1: not read yet, treated as unsigned
                input_signal_signed=i.get("input_signal_signed") == 1,
                quantity_name=_int(i.get("quantity_name"), 0),
                unit_of_measure=_int(i.get("unit_of_measure"), 0),
            )
            for i in (d.get("instances") or ())
        )
        out.append(
            InputDevice(
                name=d.get("name") or f"Input {addr}",
                address=addr,
                instances=instances,
                gtin=_parse_gtin(d.get("gtin")),
            )
        )
    return out
