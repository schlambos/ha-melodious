"""Buttons for commands on hub devices mirrored into Home Assistant.

Each enabled command is a tap-only button. Holding is not a Home Assistant
button concept; use ``remote.send_command`` on the hub's remote entity with
``hold_secs`` when a command needs to repeat while held.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .catalogue import engine_error
from .const import CONF_MIRROR_DEVICES
from .coordinator import MelodiousCoordinator
from .entity import mirror_device_info
from .hub import HubError
from .mirror import MirrorButtonSpec, mirror_button_specs, pushed_device_keys

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: MelodiousCoordinator = entry.runtime_data.coordinator
    added = False

    @callback
    def _add_buttons() -> None:
        nonlocal added
        if added or coordinator.data is None:
            return
        added = True
        specs = mirror_button_specs(
            coordinator.catalogue,
            entry.options.get(CONF_MIRROR_DEVICES, []),
            pushed_device_keys(entry.options),
        )
        async_add_entities(
            [MirrorCommandButton(coordinator, entry, spec) for spec in specs]
        )

    _add_buttons()
    if not added:
        # The hub was unreachable at setup; add the buttons once it answers.
        entry.async_on_unload(coordinator.async_add_listener(_add_buttons))


class MirrorCommandButton(CoordinatorEntity[MelodiousCoordinator], ButtonEntity):
    """One command on a mirrored hub device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MelodiousCoordinator,
        entry: ConfigEntry,
        spec: MirrorButtonSpec,
    ) -> None:
        super().__init__(coordinator)
        self._spec = spec
        key = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{key}_{spec.device.id}_button_{spec.command}"
        self._attr_name = spec.command
        self._attr_device_info = mirror_device_info(entry, spec.device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "hub_device_id": self._spec.device.id,
            "command": self._spec.command,
            "tap_only": True,
            "hold": (
                "Use remote.send_command on the hub remote with hold_secs "
                "to hold this command."
            ),
        }

    async def async_press(self) -> None:
        try:
            result = await self.coordinator.client.send_command(
                self._spec.device.id, self._spec.command
            )
        except HubError as err:
            raise HomeAssistantError(
                f"The hub didn't send {self._spec.command} to "
                f"{self._spec.device.name}: {err}"
            ) from err
        if (why := engine_error(result.get("reply"))) is not None:
            raise HomeAssistantError(
                f"The hub didn't send {self._spec.command} to "
                f"{self._spec.device.name}: {why}"
            )
