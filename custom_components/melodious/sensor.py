"""Activity sensors for the hub and its mirrored devices.

The mirrored-device sensor is diagnostic: it says which running activity the
device belongs to and the role it plays there. The hub device's sensor is the
current activity and its member devices, the same view the remote exposes.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .catalogue import HubDevice
from .const import CONF_MIRROR_DEVICES
from .coordinator import MelodiousCoordinator
from .entity import hub_device_info, hub_key, mirror_device_info
from .mirror import (
    activity_members,
    device_activity_name,
    device_activity_role,
    device_in_activity,
    mirrored_devices,
    pushed_device_keys,
    running_activity,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MelodiousCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _add_sensors() -> None:
        nonlocal added
        if added or coordinator.data is None:
            return
        added = True
        devices = mirrored_devices(
            coordinator.catalogue,
            entry.options.get(CONF_MIRROR_DEVICES, []),
            pushed_device_keys(entry.options),
        )
        entities = [
            MirrorActivitySensor(coordinator, entry, device) for device in devices
        ]
        entities.append(HubActivitySensor(coordinator, entry))
        async_add_entities(entities)

    _add_sensors()
    if not added:
        # The hub was unreachable at setup; add the sensors once it answers.
        entry.async_on_unload(coordinator.async_add_listener(_add_sensors))


class MirrorActivitySensor(CoordinatorEntity[MelodiousCoordinator], SensorEntity):
    """Which running activity a mirrored device belongs to, and its role."""

    _attr_has_entity_name = True
    _attr_name = "Activity"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: MelodiousCoordinator,
        entry: ConfigEntry,
        device: HubDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = f"{hub_key(entry)}_{device.id}_activity"
        self._attr_device_info = mirror_device_info(entry, device)

    @property
    def native_value(self) -> str:
        return device_activity_name(
            self.coordinator.catalogue, self.coordinator.activity_id, self._device_id
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        catalogue = self.coordinator.catalogue
        activity = running_activity(catalogue, self.coordinator.activity_id)
        member = device_in_activity(activity, self._device_id)
        return {
            "activity_id": activity.id if member else None,
            "role": device_activity_role(
                catalogue, self.coordinator.activity_id, self._device_id
            ),
        }


class HubActivitySensor(CoordinatorEntity[MelodiousCoordinator], SensorEntity):
    """The hub device's current activity and its member devices."""

    _attr_has_entity_name = True
    _attr_name = "Current activity"

    def __init__(
        self,
        coordinator: MelodiousCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{hub_key(entry)}_activity"
        self._attr_device_info = hub_device_info(entry)

    @property
    def native_value(self) -> str:
        return self.coordinator.catalogue.activity_name(self.coordinator.activity_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        catalogue = self.coordinator.catalogue
        activity = running_activity(catalogue, self.coordinator.activity_id)
        return {
            "activity_id": activity.id if activity is not None
            else self.coordinator.activity_id,
            "members": activity_members(catalogue, activity),
        }
