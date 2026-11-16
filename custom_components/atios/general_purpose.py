"""DALI-2 general purpose sensors (IEC 62386-306).

Pure helpers, no Home Assistant imports, unit tested standalone
(tests/test_general_purpose.py). Events are decoded like the SmartCore firmware
(lib-dali general_purpose_sensor.c) and web UI, with the instance metadata the
SmartCore stores in its NVRAM model: resolution, magnitude, signedness, quantity
name and unit of measure.
"""

from __future__ import annotations

from dataclasses import dataclass

# A measured value report has bit 9 set, the value is in bits 8..0
_EVENT_MAX = 0x3FF
_REPORT = 0x200
_EVENT_BITS = 9
_MAX_RESOLUTION = 32
_MAGNITUDE_UNITY = 127

UNIT_SYMBOLS: dict[int, str] = {
    2: "s", 3: "Hz", 4: "m", 5: "kg", 6: "V", 7: "A", 8: "K", 9: "°C", 10: "°F",
    11: "mol", 12: "°", 13: "rad", 14: "sr", 15: "N", 16: "Pa", 17: "psi", 18: "J",
    19: "W", 20: "C", 21: "F", 22: "Ω", 23: "S", 24: "Wb", 25: "T", 26: "H",
    27: "A/m", 28: "V/m", 29: "Ah", 30: "Wh", 31: "lm", 32: "lx", 33: "cd",
    34: "cd/m²", 35: "Bq", 36: "Gy", 37: "Sv", 38: "kat", 39: "m²", 40: "m³",
    41: "m/s", 42: "m/s²", 43: "m³/s", 44: "kg/m³", 45: "kg/m²", 46: "m³/kg",
    47: "ft", 48: "Nm", 49: "dB", 50: "%", 51: "‰", 52: "‱", 53: "ppm",
}  # 0 manufacturer-defined, 1 dimensionless: no unit

QUANTITY_NAMES: dict[int, str] = {
    1: "time", 2: "frequency", 3: "distance", 4: "force", 5: "weight", 6: "mass",
    7: "velocity", 8: "acceleration", 9: "angle", 10: "area", 11: "volume",
    12: "torque", 13: "voltage", 14: "current", 15: "power", 16: "apparent power",
    17: "reactive power", 18: "energy", 19: "power factor", 20: "sound pressure",
    21: "CCT", 22: "CRI", 23: "red light intensity", 24: "green light intensity",
    25: "blue light intensity", 26: "temperature", 27: "wet bulb temperature",
    28: "absolute humidity", 29: "humidity", 30: "dew point", 31: "pressure",
    32: "flow rate", 33: "SO2", 34: "CO2", 35: "CO", 36: "VOC", 37: "NOx",
    38: "N2O", 39: "NH3", 40: "ozone", 41: "chlorine", 42: "methane", 43: "pH",
    44: "PM10", 45: "PM2.5", 46: "air quality index", 50: "longitude",
    51: "latitude", 52: "altitude", 53: "position x", 54: "position y",
    55: "position z", 56: "wind speed", 57: "fluid level", 58: "battery",
}  # 0 manufacturer-defined, 47..49 beacon RSSI: no name

# Home Assistant device classes (SensorDeviceClass values) by unit, or by unit and quantity
_DEVICE_CLASS_BY_UNIT = {
    2: "duration", 3: "frequency", 4: "distance", 6: "voltage", 7: "current",
    8: "temperature", 9: "temperature", 10: "temperature", 16: "pressure",
    19: "power", 32: "illuminance", 49: "sound_pressure",
}
_DEVICE_CLASS_BY_QUANTITY = {
    (50, 29): "humidity",
    (50, 58): "battery",
    (53, 34): "carbon_dioxide",
    (53, 35): "carbon_monoxide",
    (53, 36): "volatile_organic_compounds_parts",
    (41, 56): "wind_speed",
    (41, 7): "speed",
}


@dataclass(frozen=True)
class Measurement:
    """How to read and present one general purpose instance."""

    resolution: int
    magnitude: int = _MAGNITUDE_UNITY
    signed: bool = False
    quantity_name: int = 0
    unit_of_measure: int = 0

    @property
    def unit(self) -> str | None:
        return UNIT_SYMBOLS.get(self.unit_of_measure)

    @property
    def device_class(self) -> str | None:
        key = (self.unit_of_measure, self.quantity_name)
        return _DEVICE_CLASS_BY_QUANTITY.get(key) or _DEVICE_CLASS_BY_UNIT.get(self.unit_of_measure)

    @property
    def label(self) -> str:
        return QUANTITY_NAMES.get(self.quantity_name, "sensor")

    @property
    def decimals(self) -> int:
        return max(0, _MAGNITUDE_UNITY - self.magnitude)

    def decode_event(self, event_info: int) -> float | None:
        """The measurement of a measured value report, None for other events or MASK."""
        res = self.resolution
        if not 0 < res <= _MAX_RESOLUTION or event_info > _EVENT_MAX or not event_info & _REPORT:
            return None
        # The event carries the 9 most significant bits, repeated for a higher resolution
        measured = 0
        for bit in range(res):
            measured = (measured << 1) | ((event_info >> (_EVENT_BITS - 1 - bit % _EVENT_BITS)) & 1)
        if measured > (1 << res) - 2:  # all ones: MASK, no valid value
            return None
        offset = (1 << (res - 1)) - 1 if self.signed else 0
        exponent = self.magnitude - _MAGNITUDE_UNITY
        value = (measured - offset) * 10.0**exponent
        return round(value, self.decimals) if exponent < 0 else value
