"""The hub as a remote entity, like the built-in Harmony integration's (#87).

remote.send_command sends any hub device's command (IR, Bluetooth or Home
Assistant-backed) by device and command name; remote.turn_on/turn_off start
an activity or PowerOff. Learning and deleting commands stay in the
Melodious app, so those features are not advertised.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

from homeassistant.components.remote import (
    ATTR_ACTIVITY,
    ATTR_DELAY_SECS,
    ATTR_DEVICE,
    ATTR_HOLD_SECS,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    RemoteEntity,
    RemoteEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .catalogue import POWER_OFF, Catalogue, engine_error, resolve_command
from .coordinator import MelodiousCoordinator
from .entity import hub_device_info
from .hub import HubClient, HubError

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
HOLD_REFRESH_SECONDS = 0.2
ATTR_LAST_ACTIVITY = "last_activity"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([MelodiousRemote(entry.runtime_data.coordinator, entry)])


class MelodiousRemote(CoordinatorEntity[MelodiousCoordinator], RemoteEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = RemoteEntityFeature.ACTIVITY

    def __init__(self, coordinator: MelodiousCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_remote"
        self._attr_device_info = hub_device_info(entry)
        self._last_activity: str | None = None

    @property
    def _catalogue(self) -> Catalogue:
        return self.coordinator.catalogue

    @property
    def _activity_id(self) -> str | None:
        return self.coordinator.activity_id

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def current_activity(self) -> str | None:
        return self._catalogue.activity_name(self._activity_id)

    @property
    def activity_list(self) -> list[str]:
        return [a.name for a in self._catalogue.activities]

    @property
    def is_on(self) -> bool:
        return self.current_activity != POWER_OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "devices": [d.name for d in self._catalogue.devices],
            ATTR_LAST_ACTIVITY: self._last_activity,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._last_activity = last.attributes.get(ATTR_LAST_ACTIVITY)
        self._remember_activity()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._remember_activity()
        super()._handle_coordinator_update()

    def _remember_activity(self) -> None:
        if self.coordinator.data is not None and self.current_activity != POWER_OFF:
            self._last_activity = self.current_activity

    async def async_turn_on(self, **kwargs: Any) -> None:
        catalogue = self._catalogue
        wanted = kwargs.get(ATTR_ACTIVITY) or self._last_activity
        activity = catalogue.activity(wanted) if wanted else None
        if activity is None and not kwargs.get(ATTR_ACTIVITY) and catalogue.activities:
            activity = catalogue.activities[0]
        if activity is None:
            _LOGGER.error("No activity %r on the hub; it has: %s", wanted,
                          ", ".join(self.activity_list) or "none")
            return
        if activity.id == self._activity_id:
            _LOGGER.debug("%s is already running", activity.name)
            return
        await self._run(activity.id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run("-1")

    async def _run(self, activity_id: str) -> None:
        try:
            await self.coordinator.client.run_activity(activity_id)
        except HubError as err:
            raise HomeAssistantError(f"The hub didn't start the activity: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        device_name = kwargs.get(ATTR_DEVICE)
        if not device_name:
            _LOGGER.error("send_command needs a device; the hub has: %s", self._device_names())
            return
        device = self._catalogue.device(device_name)
        if device is None:
            _LOGGER.error("No device %r on the hub; it has: %s", device_name, self._device_names())
            return
        names = []
        for item in command:
            name = resolve_command(device, item)
            if name is None:
                _LOGGER.error("%s has no command %r; it has: %s", device.name, item,
                              ", ".join(device.commands[:60]))
            else:
                names.append(name)
        if not names:
            return
        repeats = max(1, int(kwargs.get(ATTR_NUM_REPEATS) or 1))
        delay = max(0.0, float(kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS) or 0))
        hold = max(0.0, float(kwargs.get(ATTR_HOLD_SECS) or 0))
        if hold and device.is_ha:
            _LOGGER.warning("%s is a Home Assistant device: holding repeats its command, "
                            "which can fire a toggle more than once", device.name)
        client = self.coordinator.client
        total = repeats * len(names)
        sent = 0
        for _ in range(repeats):
            for name in names:
                try:
                    if hold:
                        await self._hold(client, device.id, name, hold)
                    else:
                        result = await client.send_command(device.id, name)
                        if (why := engine_error(result.get("reply"))) is not None:
                            _LOGGER.warning("The hub didn't send %s to %s: %s", name, device.name, why)
                except HubError as err:
                    raise HomeAssistantError(f"The hub didn't send {name} to {device.name}: {err}") from err
                sent += 1
                if delay and sent < total:
                    await asyncio.sleep(delay)

    async def _hold(self, client: HubClient, device_id: str, name: str, seconds: float) -> None:
        """Press, keep the hub's hold alive (its lease is 700 ms), then release."""
        hold_id = uuid.uuid4().hex
        await client.send_command(device_id, name, "press", hold_id)
        loop = asyncio.get_running_loop()
        end = loop.time() + seconds
        try:
            while (remaining := end - loop.time()) > 0:
                await asyncio.sleep(min(HOLD_REFRESH_SECONDS, remaining))
                if end - loop.time() > 0:
                    await client.send_command(device_id, name, "hold", hold_id)
        finally:
            with suppress(HubError):
                await client.send_command(device_id, name, "release", hold_id)

    def _device_names(self) -> str:
        return ", ".join(d.name for d in self._catalogue.devices) or "none"
