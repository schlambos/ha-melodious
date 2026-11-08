"""Device-registry identities for the hub and the devices mirrored from it."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.device_registry import DeviceInfo

from .catalogue import HubDevice
from .const import DOMAIN


def hub_key(entry: ConfigEntry) -> str:
    """The stable per-hub key used by every entity's unique_id."""
    return entry.unique_id or entry.entry_id


def hub_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, hub_key(entry))},
        manufacturer="Melodious",
        model="Harmony Hub",
        name=entry.title,
        configuration_url=f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}",
    )


def mirror_device_info(entry: ConfigEntry, device: HubDevice) -> DeviceInfo:
    """A hub-only device, stable across renames and unique per hub.

    Home Assistant identifiers are (domain, identifier) pairs, so the hub host
    and the hub's numeric device id are joined into one identifier. The hub's
    Id- never changes when the device is renamed.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.data[CONF_HOST]}:{device.id}")},
        manufacturer=device.manufacturer or "Harmony",
        model=device.model,
        name=device.name,
        via_device=(DOMAIN, hub_key(entry)),
    )
