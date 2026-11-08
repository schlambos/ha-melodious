"""Rules for hub devices mirrored into Home Assistant (hub -> HA).

Pure functions only, so the unit tests load this file without Home Assistant.
The integration stores only the user's choices in options; device metadata and
commands always come from the hub's current /api/activity-config.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalogue import Catalogue, HubDevice
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
