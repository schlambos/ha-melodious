"""Rules for hub devices mirrored into Home Assistant (hub -> HA).

Pure functions only, so the unit tests load this file without Home Assistant.
The integration stores only the user's choices in options; device metadata and
commands always come from the hub's current /api/activity-config.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalogue import (
    POWER_OFF,
    Catalogue,
    HubActivity,
    HubDevice,
    resolve_command,
)
from .const import CONF_DEVICES, CONF_ENTITIES


@dataclass(frozen=True)
class MirrorButtonSpec:
    """One command on one mirrored hub device."""

    device: HubDevice
    command: str


def device_label(device: HubDevice) -> str:
    """A picker label that shows the hardware, not just the hub's name."""
    hardware = " ".join(part for part in (device.manufacturer, device.model) if part)
    return f"{device.name} ({hardware})" if hardware else device.name


def pushed_device_keys(integration_options: dict | None) -> set[str]:
    """The hub keys and entity IDs this integration itself pushed to the hub."""
    keys = {
        str(entity)
        for entity in (integration_options or {}).get(CONF_ENTITIES, []) or []
    }
    for item in (integration_options or {}).get(CONF_DEVICES, []) or []:
        if item.get("key"):
            keys.add(str(item["key"]))
        keys.update(str(entity) for entity in item.get("entities") or [])
    return keys


def is_pushed_device(device: HubDevice, pushed_keys: set[str] | None) -> bool:
    return bool(device.key and pushed_keys and device.key in pushed_keys)


def mirror_picker_options(
    catalogue: Catalogue, integration_options: dict | None = None
) -> list[dict[str, str]]:
    """Options for the Configure mirror picker: hub-only devices only."""
    pushed = pushed_device_keys(integration_options)
    return [
        {"value": device.id, "label": device_label(device)}
        for device in sorted(catalogue.mirror_devices(), key=lambda d: d.name.casefold())
        if not is_pushed_device(device, pushed)
    ]


def mirror_device_config(
    mirror_options: list[dict] | None, device_id: str
) -> dict | None:
    """The stored options entry for one mirrored device, if it is still picked."""
    text = str(device_id)
    return next(
        (item for item in mirror_options or [] if str(item.get("device_id")) == text),
        None,
    )


def enabled_commands(device: HubDevice, disabled_commands: list[str] | None) -> list[str]:
    """The device's commands minus the ones the user disabled in Configure."""
    disabled = {str(name).casefold() for name in disabled_commands or []}
    return [command for command in device.commands if command.casefold() not in disabled]


def mirror_button_specs(
    catalogue: Catalogue,
    mirror_options: list[dict] | None,
    pushed_keys: set[str] | None = None,
) -> list[MirrorButtonSpec]:
    """One button per enabled command on every mirrored device.

    A stored device that no longer exists on the hub, that the hub now marks
    as Home Assistant-backed, or that this integration itself pushed is
    ignored. Nothing is mirrored back.
    """
    specs: list[MirrorButtonSpec] = []
    for item in mirror_options or []:
        device = catalogue.device_by_id(str(item.get("device_id", "")))
        if device is None or device.is_ha or is_pushed_device(device, pushed_keys):
            continue
        for command in enabled_commands(device, item.get("disabled_commands")):
            specs.append(MirrorButtonSpec(device, command))
    return specs


def mirrored_devices(
    catalogue: Catalogue,
    mirror_options: list[dict] | None,
    pushed_keys: set[str] | None = None,
) -> list[HubDevice]:
    """The stored picks that still exist and may be mirrored.

    The same rules as the buttons: a device removed from the hub, marked as
    Home Assistant-backed, or pushed by this integration is dropped.
    """
    devices: list[HubDevice] = []
    for item in mirror_options or []:
        device = catalogue.device_by_id(str(item.get("device_id", "")))
        if device is None or device.is_ha or is_pushed_device(device, pushed_keys):
            continue
        devices.append(device)
    return devices


def mirror_power_devices(
    catalogue: Catalogue,
    mirror_options: list[dict] | None,
    pushed_keys: set[str] | None = None,
) -> list[HubDevice]:
    """Mirrored devices with a power feature (a switch)."""
    return [
        device for device in mirrored_devices(catalogue, mirror_options, pushed_keys)
        if device.power_type is not None
    ]


def mirror_input_devices(
    catalogue: Catalogue,
    mirror_options: list[dict] | None,
    pushed_keys: set[str] | None = None,
) -> list[HubDevice]:
    """Mirrored devices with at least one input (a source select)."""
    return [
        device for device in mirrored_devices(catalogue, mirror_options, pushed_keys)
        if device.inputs
    ]


def running_activity(catalogue: Catalogue, activity_id: str | None) -> HubActivity | None:
    """The activity the hub is running, or None for PowerOff/unknown."""
    if activity_id in (None, "", "-1"):
        return None
    return catalogue.activity(str(activity_id))


def device_in_activity(activity: HubActivity | None, device_id: str) -> bool:
    return activity is not None and str(device_id) in activity.device_ids


def device_power_on(
    catalogue: Catalogue, activity_id: str | None, device_id: str
) -> bool:
    """Optimistic power: on exactly when the device is in the running activity."""
    return device_in_activity(running_activity(catalogue, activity_id), device_id)


def device_activity_name(
    catalogue: Catalogue, activity_id: str | None, device_id: str
) -> str:
    """The running activity this device is part of, or "Idle"."""
    activity = running_activity(catalogue, activity_id)
    return activity.name if device_in_activity(activity, device_id) else "Idle"


def device_activity_role(
    catalogue: Catalogue, activity_id: str | None, device_id: str
) -> str | None:
    """The device's role in the running activity it belongs to, or None."""
    activity = running_activity(catalogue, activity_id)
    if not device_in_activity(activity, device_id):
        return None
    return activity.role_for(device_id)


def activity_members(catalogue: Catalogue, activity: HubActivity | None) -> dict[str, str]:
    """Device name -> role for an activity, for the hub activity sensor."""
    if activity is None:
        return {}
    members: dict[str, str] = {}
    for role, device_id in activity.roles:
        device = catalogue.device_by_id(device_id)
        members[device.name if device is not None else device_id] = role
    return members


def input_command(device: HubDevice, input_name: str) -> str | None:
    """The device's command for an input, in the hub's own spelling.

    Input names lose their spaces (``HDMI Arc`` -> ``InputHDMIArc``) and the
    match is case-insensitive because the hub spells it ``InputHdmiArc``.
    """
    return resolve_command(device, "Input" + str(input_name).replace(" ", ""))


def activity_options(catalogue: Catalogue) -> list[str]:
    """Activity names plus PowerOff, for the hub device's activity select."""
    return [activity.name for activity in catalogue.activities] + [POWER_OFF]
