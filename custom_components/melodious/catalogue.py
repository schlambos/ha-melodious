"""The hub's devices, commands and activities, for the hub's remote entity.

Pure rules, no Home Assistant imports, so tests load this file directly.
Names are matched the way the built-in Harmony integration matches them:
exact name, then numeric ID. A case-insensitive name match is a last resort.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

POWER_OFF = "PowerOff"
HA_TRANSPORT = 64
POWER_DISCRETE = 1
POWER_TOGGLE = 2


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
    # DeviceFeatures.PowerFeature: 1 discrete, 2 toggle, None no power.
    power_type: int | None = None
    power_on_command: str | None = None
    power_off_command: str | None = None
    # DeviceFeatures.InputFeature names, or None when the device has none.
    inputs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class HubActivity:
    id: str
    name: str
    # Roles[].DeviceId- values, in the activity's order.
    device_ids: tuple[str, ...] = ()
    # (Roles[].__type, Roles[].DeviceId-) pairs.
    roles: tuple[tuple[str, str], ...] = ()

    def role_for(self, device_id: str) -> str | None:
        """The role this device plays in the activity, or None."""
        text = str(device_id)
        return next((role for role, member in self.roles if member == text), None)


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
            features = entry.get("DeviceFeatures") or []
            if not isinstance(features, list):
                features = []
            power_type, power_on, power_off = _power_fields(features)
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
                power_type=power_type,
                power_on_command=power_on,
                power_off_command=power_off,
                inputs=_input_names(features),
            ))
        activities = []
        for activity in (config.get("activityList") or {}).get("Activities") or []:
            if activity.get("Id-") is None or not activity.get("Name"):
                continue
            if str(activity["Id-"]) == "-1":
                continue
            roles = []
            for role in activity.get("Roles") or []:
                if not isinstance(role, dict) or role.get("DeviceId-") is None:
                    continue
                roles.append((
                    str(role.get("__type") or ""),
                    str(role["DeviceId-"]),
                ))
            # One membership entry per device, keeping the activity's order.
            device_ids = tuple(dict.fromkeys(member for _, member in roles))
            activities.append(HubActivity(
                id=str(activity["Id-"]),
                name=str(activity["Name"]),
                device_ids=device_ids,
                roles=tuple(roles),
            ))
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


def _first_ir_command(actions) -> str | None:
    """The first IRPressAction's command name, or None."""
    for action in actions or []:
        if isinstance(action, dict) and action.get("IRCommandName"):
            return str(action["IRCommandName"])
    return None


def _power_fields(features: list) -> tuple[int | None, str | None, str | None]:
    """(PowerTypeId, on command, off command) from DeviceFeatures."""
    feature = next(
        (f for f in features
         if isinstance(f, dict) and f.get("__type") == "PowerFeature"),
        None,
    )
    if feature is None:
        return None, None, None
    power_type = feature.get("PowerTypeId")
    if type(power_type) is not int or power_type not in (POWER_DISCRETE, POWER_TOGGLE):
        return None, None, None
    if power_type == POWER_TOGGLE:
        command = _first_ir_command(feature.get("PowerToggleActions"))
        return power_type, command, command
    return (
        power_type,
        _first_ir_command(feature.get("PowerOnActions")),
        _first_ir_command(feature.get("PowerOffActions")),
    )


def _input_names(features: list) -> tuple[str, ...] | None:
    """InputFeature input names, dropping null entries; None when empty."""
    feature = next(
        (f for f in features
         if isinstance(f, dict) and f.get("__type") == "InputFeature"),
        None,
    )
    if feature is None:
        return None
    names = tuple(
        str(item["InputName"])
        for item in feature.get("Inputs") or []
        if isinstance(item, dict) and item.get("InputName")
    )
    return names or None


def extract_state_version(reply) -> str | None:
    """The stateVersion inside an /api/state reply, however deeply escaped."""
    if reply is None:
        return None
    raw = reply if isinstance(reply, str) else json.dumps(reply)
    # The engine wraps the digest as a JSON string; drop the escaping so one
    # expression finds stateVersion at any nesting depth, like the web UI.
    match = re.search(r'"stateVersion"\s*:\s*"?(\d+)', raw.replace("\\", ""), re.IGNORECASE)
    return match.group(1) if match else None


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
