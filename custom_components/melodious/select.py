"""Input and activity selects for the hub and its mirrored devices.

The input select is optimistic: the hub cannot know which input an IR device
is actually on, so a successful send is assumed and an activity change resets
it to unknown. The hub device's activity select starts activities through the
same endpoint as the remote entity.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .catalogue import POWER_OFF, HubDevice, engine_error
from .const import CONF_MIRROR_DEVICES
from .coordinator import MelodiousCoordinator
from .entity import hub_device_info, hub_key, mirror_device_info
from .hub import HubError
from .mirror import (
    activity_options,
    input_command,
    mirror_input_devices,
    pushed_device_keys,
)

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MelodiousCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _add_selects() -> None:
        nonlocal added
        if added or coordinator.data is None:
            return
        added = True
        devices = mirror_input_devices(
            coordinator.catalogue,
            entry.options.get(CONF_MIRROR_DEVICES, []),
            pushed_device_keys(entry.options),
        )
        entities = [
            MirrorInputSelect(coordinator, entry, device) for device in devices
        ]
        entities.append(HubActivitySelect(coordinator, entry))
        async_add_entities(entities)

    _add_selects()
    if not added:
        # The hub was unreachable at setup; add the selects once it answers.
        entry.async_on_unload(coordinator.async_add_listener(_add_selects))


class MirrorInputSelect(CoordinatorEntity[MelodiousCoordinator], SelectEntity):
    """One mirrored hub device's input, optimistic and reset on activity change."""

    _attr_has_entity_name = True
    _attr_name = "Input"

    def __init__(
        self,
        coordinator: MelodiousCoordinator,
        entry: ConfigEntry,
        device: HubDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._initial_device = device
        self._attr_options = list(device.inputs or ())
        self._attr_current_option = None
        self._last_activity_id = coordinator.activity_id
        self._attr_unique_id = f"{hub_key(entry)}_{device.id}_input"
        self._attr_device_info = mirror_device_info(entry, device)

    @property
    def _device(self) -> HubDevice:
        return (
            self.coordinator.catalogue.device_by_id(self._device_id)
            or self._initial_device
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        # The hub doesn't know the input after an activity change; forget the
        # optimistic pick instead of claiming it is still selected.
        activity_id = self.coordinator.activity_id
        if activity_id != self._last_activity_id:
            self._last_activity_id = activity_id
            self._attr_current_option = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError(
                f"{self._device.name} has no input {option!r}"
            )
        device = self._device
        command = input_command(device, option)
        if command is None:
            _LOGGER.warning(
                "%s has no input command for %r; its commands are: %s",
                device.name,
                option,
                ", ".join(device.commands[:60]),
            )
            raise HomeAssistantError(
                f"The hub has no command to select {option!r} on {device.name}"
            )
        try:
            result = await self.coordinator.client.send_command(device.id, command)
        except HubError as err:
            raise HomeAssistantError(
                f"The hub didn't select {option} on {device.name}: {err}"
            ) from err
        if (why := engine_error(result.get("reply"))) is not None:
            raise HomeAssistantError(
                f"The hub didn't select {option} on {device.name}: {why}"
            )
        self._attr_current_option = option
        self.async_write_ha_state()


class HubActivitySelect(CoordinatorEntity[MelodiousCoordinator], SelectEntity):
    """The hub device's activity picker, including PowerOff."""

    _attr_has_entity_name = True
    _attr_name = "Activity"

    def __init__(
        self,
        coordinator: MelodiousCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{hub_key(entry)}_activity_select"
        self._attr_device_info = hub_device_info(entry)

    @property
    def options(self) -> list[str]:
        return activity_options(self.coordinator.catalogue)

    @property
    def current_option(self) -> str:
        return self.coordinator.catalogue.activity_name(self.coordinator.activity_id)

    async def async_select_option(self, option: str) -> None:
        if option == POWER_OFF:
            activity_id = "-1"
        else:
            activity = self.coordinator.catalogue.activity(option)
            if activity is None:
                raise HomeAssistantError(f"The hub has no activity {option!r}")
            activity_id = activity.id
        try:
            await self.coordinator.client.run_activity(activity_id)
        except HubError as err:
            raise HomeAssistantError(
                f"The hub didn't start {option}: {err}"
            ) from err
        await self.coordinator.async_request_refresh()
