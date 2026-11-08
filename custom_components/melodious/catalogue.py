"""The hub's devices, commands and activities, for the hub's remote entity.

Pure rules, no Home Assistant imports, so tests load this file directly.
Names are matched the way the built-in Harmony integration matches them:
exact name, then numeric ID. A case-insensitive name match is a last resort.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

POWER_OFF = "PowerOff"
HA_TRANSPORT = 64


@dataclass(frozen=True)
class HubDevice:
    id: str
    name: str
    commands: tuple[str, ...]
    is_ha: bool
    manufacturer: str | None = None
    model: str | None = None
    transport: int | None = None
    ha_tag: str | None = None
    # The {d} token an HA-backed device carries (its integration key).
    key: str | None = None


@dataclass(frozen=True)
class HubActivity:
    id: str
    name: str


@dataclass(frozen=True)
class Catalogue:
    devices: tuple[HubDevice, ...]
    activities: tuple[HubActivity, ...]

    @classmethod
    def from_config(cls, config: dict) -> "Catalogue":
        """From GET /api/activity-config. Commands sit beside Device, not in it."""
        devices = []
        for entry in (config.get("deviceList") or {}).get("DevicesWithFeatures") or []:
            device = entry.get("Device") or {}
            if device.get("Id-") is None or not device.get("Name"):
                continue
            commands = entry.get("Commands") or []
            if not isinstance(commands, list):
                commands = []
            transport = device.get("Transport")
            if type(transport) is not int:
                transport = None
            ha_tag = str(device.get("haTag") or "") or None
            auto = device.get("AutoDetectedDevice")
            auto_id = str(auto.get("Id") or "") if isinstance(auto, dict) else ""
            tokens = device.get("Tokens")
            tokens = tokens.get("Parameters") if isinstance(tokens, dict) else []
            key = next(
                (str(token.get("Value")) for token in tokens
                 if isinstance(token, dict) and token.get("Key") == "{d}"
                 and token.get("Value")),
                None,
            )
            devices.append(HubDevice(
                id=str(device["Id-"]),
                name=str(device["Name"]),
                commands=tuple(str(c["Name"]) for c in commands if c.get("Name")),
                # The hub marks devices the integration pushed with a
                # melodious-ha-* AutoDetectedDevice id and TransportType 64.
                # Either marker (or a haTag) means "never mirror this back".
                is_ha=bool(ha_tag) or transport == HA_TRANSPORT
                or auto_id.startswith("melodious-ha-")
                or any(
                    isinstance(c, dict) and c.get("TransportType") == HA_TRANSPORT
                    for c in commands
                ),
                manufacturer=str(device.get("Manufacturer") or "") or None,
                model=str(device.get("Model") or "") or None,
                transport=transport,
                ha_tag=ha_tag,
                key=key,
            ))
        activities = []
        for activity in (config.get("activityList") or {}).get("Activities") or []:
            if activity.get("Id-") is None or not activity.get("Name"):
                continue
            if str(activity["Id-"]) == "-1":
                continue
            activities.append(HubActivity(id=str(activity["Id-"]), name=str(activity["Name"])))
        return cls(tuple(devices), tuple(activities))

    def device(self, name_or_id: str) -> HubDevice | None:
        return _resolve(self.devices, name_or_id)

    def device_by_id(self, device_id: str) -> HubDevice | None:
        text = str(device_id)
        return next((d for d in self.devices if d.id == text), None)

    def mirror_devices(self) -> tuple[HubDevice, ...]:
        """Hub-only devices that may be mirrored into Home Assistant."""
        return tuple(d for d in self.devices if not d.is_ha)

    def activity(self, name_or_id: str) -> HubActivity | None:
        return _resolve(self.activities, name_or_id)

    def activity_name(self, activity_id: str | None) -> str:
        """The running activity's name; PowerOff for none, like the built-in Harmony."""
        if activity_id in (None, "", "-1"):
            return POWER_OFF
        found = next((a for a in self.activities if a.id == activity_id), None)
        return found.name if found else activity_id


def _resolve(items, name_or_id: str):
    text = str(name_or_id).strip()
    for item in items:
        if item.name == text:
            return item
    for item in items:
        if item.id == text:
            return item
    folded = text.casefold()
    for item in items:
        if item.name.casefold() == folded:
            return item
    return None


def resolve_command(device: HubDevice, command: str) -> str | None:
    """The device's own spelling of a command: exact, then case-insensitive."""
    text = str(command).strip()
    if text in device.commands:
        return text
    folded = text.casefold()
    return next((c for c in device.commands if c.casefold() == folded), None)


def engine_reply(raw: str | None) -> dict:
    """The engine's reply that the hub wraps as a JSON string; {} if unreadable."""
    try:
        reply = json.loads(raw or "")
    except ValueError:
        return {}
    return reply if isinstance(reply, dict) else {}


def current_activity_id(state: dict) -> str | None:
    """The running activity's ID from GET /api/activity-state, or None."""
    result = (engine_reply(state.get("reply")).get("data") or {}).get("result")
    return None if result is None else str(result)


def engine_error(raw: str | None) -> str | None:
    """Why the engine refused a command, or None when it took it."""
    reply = engine_reply(raw)
    code = reply.get("code")
    if code in (None, 200, "200"):
        return None
    return str(reply.get("msg") or f"engine code {code}")
