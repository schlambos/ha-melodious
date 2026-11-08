"""Optimistic power switches for hub devices mirrored into Home Assistant.

The hub never gets real confirmation from an IR device, so the state is
derived from activity membership: a device is on exactly when it is a member
of the running activity. PowerOff (or no activity) means everything is off.
A command send does not change the switch directly; the next state-version
poll updates it when the hub's activity state moves.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .catalogue import HubDevice, engine_error
from .const import CONF_MIRROR_DEVICES
from .coordinator import MelodiousCoordinator
from .entity import hub_key, mirror_device_info
from .hub import HubError
from .mirror import (
    device_power_on,
    mirror_power_devices,
    pushed_device_keys,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MelodiousCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _add_switches() -> None:
        nonlocal added
        if added or coordinator.data is None:
            return
        added = True
        devices = mirror_power_devices(
            coordinator.catalogue,
            entry.options.get(CONF_MIRROR_DEVICES, []),
            pushed_device_keys(entry.options),
        )
        async_add_entities(
            [MirrorPowerSwitch(coordinator, entry, device) for device in devices]
        )

    _add_switches()
    if not added:
        # The hub was unreachable at setup; add the switches once it answers.
        entry.async_on_unload(coordinator.async_add_listener(_add_switches))


class MirrorPowerSwitch(CoordinatorEntity[MelodiousCoordinator], SwitchEntity):
    """One mirrored hub device's power, derived from its activity membership."""

    _attr_has_entity_name = True
    _attr_name = "Power"
    _attr_assumed_state = True

    def __init__(
        self,
        coordinator: MelodiousCoordinator,
        entry: ConfigEntry,
        device: HubDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._initial_device = device
        self._attr_unique_id = f"{hub_key(entry)}_{device.id}_switch"
        self._attr_device_info = mirror_device_info(entry, device)

    @property
    def _device(self) -> HubDevice:
        """The hub's current view of the device, falling back to setup time."""
        return (
            self.coordinator.catalogue.device_by_id(self._device_id)
            or self._initial_device
        )

    @property
    def is_on(self) -> bool:
        return device_power_on(
            self.coordinator.catalogue, self.coordinator.activity_id, self._device_id
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "hub_device_id": self._device_id,
            "power_type": self._device.power_type,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(self._device.power_on_command, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(self._device.power_off_command, "off")

    async def _send(self, command: str | None, direction: str) -> None:
        device = self._device
        if not command:
            raise HomeAssistantError(
                f"{device.name} has no power {direction} command on the hub"
            )
        try:
            result = await self.coordinator.client.send_command(device.id, command)
        except HubError as err:
            raise HomeAssistantError(
                f"The hub didn't send {command} to {device.name}: {err}"
            ) from err
        if (why := engine_error(result.get("reply"))) is not None:
            raise HomeAssistantError(
                f"The hub didn't send {command} to {device.name}: {why}"
            )
