"""Melodious: Home Assistant entities as devices on a Melodious Harmony hub.

Each exposed entity becomes an ordinary Harmony device on the hub, so it can
sit in activities and on remote buttons. Its commands are IP commands: the
hub's engine posts {"device": <entity_id>, "command": <value>} to this
integration's webhook, which runs the matching service call. No Home
Assistant token lives on the hub; the webhook ID is the only secret.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, STATE_UNAVAILABLE, Platform
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import CONF_DEVICES, CONF_ENTITIES, CONF_HA_URL, CONF_WEBHOOK_ID, DOMAIN, PUSH_DELAY_SECONDS, RETRY_SECONDS
from .devices import (
    DeviceGroup,
    Entity,
    allowed_values,
    build_device_set,
    build_merged_set,
    resolve_v2_command,
    service_call,
)
from .hub import HubClient, HubError, HubRefused

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.REMOTE]

type MelodiousConfigEntry = ConfigEntry[MelodiousRuntime]


def hub_client(hass: HomeAssistant, data: dict[str, Any]) -> HubClient:
    return HubClient(
        async_get_clientsession(hass),
        data[CONF_HOST],
        data[CONF_PORT],
        data.get(CONF_USERNAME) or None,
        data.get(CONF_PASSWORD) or None,
    )


class MelodiousRuntime:
    """Keeps the hub's Home Assistant devices in step with the exposed entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.client = hub_client(hass, entry.data)
        self.store: Store[dict] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        # The set the hub last took, and the commands a webhook call may run.
        self.sent: dict | None = None
        self._hub_seed: dict | None = None
        self.allowed: dict[str, set[str]] = {}
        self.taken_names: set[str] = set()
        self._lock = asyncio.Lock()
        self._timer: CALLBACK_TYPE | None = None

    @property
    def webhook_url(self) -> str:
        return f"{self.entry.data[CONF_HA_URL].rstrip('/')}/api/webhook/{self.entry.data[CONF_WEBHOOK_ID]}"

    async def async_load(self) -> None:
        self.sent = await self.store.async_load()
        self.allowed = allowed_values(self.sent or {})

    def entities(self) -> list[Entity]:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        out = []
        for entity_id in self.entry.options.get(CONF_ENTITIES, []):
            state = self.hass.states.get(entity_id)
            reg_entry = registry.async_get(entity_id)
            device_id = reg_entry.device_id if reg_entry else None
            platform = reg_entry.platform if reg_entry else None
            if state is None:
                out.append(Entity(entity_id, entity_id, available=False,
                                 device_id=device_id, platform=platform))
                continue
            out.append(
                Entity(
                    entity_id,
                    state.name,
                    # Scenes, scripts and buttons sit at "unknown" until used.
                    available=state.state != STATE_UNAVAILABLE,
                    supported_features=int(state.attributes.get("supported_features") or 0),
                    attributes=dict(state.attributes),
                    device_id=device_id,
                    platform=platform,
                )
            )
        return out

    def groups(self) -> list[DeviceGroup]:
        """The selected HA devices expanded through the entity registry."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        from homeassistant.helpers.entity_registry import async_entries_for_device

        groups = []
        for item in self.entry.options.get(CONF_DEVICES, []):
            device_id = item.get("device_id")
            device = dev_reg.async_get(device_id) if device_id else None
            name = (device.name_by_user or device.name) if device else item.get("device_id")
            manufacturer = device.manufacturer if device else None
            model = device.model if device else None
            wanted = set(item.get("entities") or [])
            members = []
            for reg_entry in async_entries_for_device(ent_reg, device_id):
                if wanted and reg_entry.entity_id not in wanted:
                    continue
                state = self.hass.states.get(reg_entry.entity_id)
                members.append(Entity(
                    reg_entry.entity_id,
                    state.name if state else reg_entry.entity_id,
                    available=False if state is None else state.state != STATE_UNAVAILABLE,
                    supported_features=int((state.attributes.get("supported_features") or 0))
                    if state else 0,
                    attributes=dict(state.attributes) if state else {},
                    device_id=device_id,
                    platform=reg_entry.platform,
                ))
            groups.append(DeviceGroup(device_id, name or device_id, manufacturer, model, members))
        return groups

    def _previous(self) -> dict[str, dict]:
        """The last sent set indexed by key and by entity."""
        previous: dict[str, dict] = {}
        source = self.sent or self._hub_seed
        for device in (source or {}).get("devices", []):
            if device.get("key"):
                previous[device["key"]] = device
                for item in device.get("entities", []):
                    previous.setdefault(item["entity"], device)
            elif device.get("entity"):
                previous[device["entity"]] = device
        return previous

    def _device_keys(self) -> dict[str, str]:
        keys = {}
        for item in self.entry.options.get(CONF_DEVICES, []):
            if item.get("device_id") and item.get("key"):
                keys[item["device_id"]] = item["key"]
        return keys

    def _disabled(self) -> dict[str, set[str]]:
        return {
            item["device_id"]: set(item.get("disabled_commands") or [])
            for item in self.entry.options.get(CONF_DEVICES, [])
            if item.get("device_id")
        }

    def _build(self, warnings: list[str] | None = None) -> dict:
        previous = self._previous()
        groups = self.groups()
        if groups:
            device_set, _ = build_merged_set(
                self.webhook_url, groups, self._device_keys(),
                self._disabled(), previous, self.taken_names, warnings,
            )
            return device_set
        return build_device_set(self.webhook_url, self.entities(), previous, self.taken_names)

    async def async_push(self, force: bool = False) -> None:
        """Send the set to the hub. The hub saves nothing when it hasn't changed."""
        self._cancel_timer()
        warnings: list[str] = []
        async with self._lock:
            try:
                self.taken_names = await self.client.device_names()
                if self.sent is None and self._hub_seed is None:
                    try:
                        self._hub_seed = {"devices": await self.client.ha_devices()}
                    except HubError:
                        self._hub_seed = {"devices": []}
                groups = self.groups()
                if groups:
                    try:
                        features = await self.client.features()
                    except HubError:
                        features = {"ha-devices-v2"}
                    if "ha-devices-v2" not in features:
                        persistent_notification.async_create(
                            self.hass,
                            "Update Melodious on the hub to merge devices; "
                            "sending one device per entity until then.",
                            title="Melodious",
                            notification_id=f"{DOMAIN}_{self.entry.entry_id}_v2",
                        )
                        flat = [m for g in groups for m in g.entities]
                        device_set = build_device_set(
                            self.webhook_url, flat, self._previous(), self.taken_names)
                    else:
                        persistent_notification.async_dismiss(
                            self.hass, f"{DOMAIN}_{self.entry.entry_id}_v2")
                        device_set = self._build(warnings)
                else:
                    device_set = self._build(warnings)
                for warning in warnings:
                    _LOGGER.warning("Home Assistant device trimmed: %s", warning)
                if device_set == self.sent and not force:
                    return
                result = await self.client.commit(device_set)
            except HubRefused as err:
                _LOGGER.warning("The hub refused the Home Assistant devices: %s", err)
                persistent_notification.async_create(
                    self.hass,
                    f"The hub didn't take the change: {err}",
                    title="Melodious",
                    notification_id=f"{DOMAIN}_{self.entry.entry_id}",
                )
                return
            except HubError as err:
                _LOGGER.warning("Could not update the hub's devices, trying again in %ss: %s", RETRY_SECONDS, err)
                self._timer = async_call_later(self.hass, RETRY_SECONDS, self._timer_fired)
                return
            self.sent = device_set
            self.allowed = allowed_values(device_set)
            await self.store.async_save(device_set)
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{self.entry.entry_id}")
            if result.get("changed"):
                _LOGGER.info(
                    "Hub devices: added %s, updated %s, removed %s",
                    result.get("added") or [], result.get("updated") or [], result.get("removed") or [],
                )

    @callback
    def _timer_fired(self, _now: Any) -> None:
        self._timer = None
        self.entry.async_create_background_task(self.hass, self.async_push(), f"{DOMAIN} push")

    @callback
    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None

    @callback
    def async_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        """Push again only when the set itself would change, not on every state update."""
        if self._timer is None and self.sent is not None and self._build() != self.sent:
            self._timer = async_call_later(self.hass, PUSH_DELAY_SECONDS, self._timer_fired)

    def _find_device(self, device_key: str) -> dict | None:
        for device in (self.sent or {}).get("devices", []):
            if device.get("key") == device_key or device.get("entity") == device_key:
                return device
        return None

    @callback
    def run(self, device_key: str, value: str) -> None:
        """Run one webhook call, without waiting: the hub drops IP commands queued over 500 ms."""
        if value not in self.allowed.get(device_key, set()):
            _LOGGER.warning("Ignoring %s for %s: not a command Melodious set up", value, device_key)
            return
        device = self._find_device(device_key)
        entity_id, action, command_name = device_key, value, value
        if device is not None and device.get("key"):
            resolved = resolve_v2_command(device, value)
            if resolved is None:
                _LOGGER.warning("Ignoring %s for %s: unknown alias", value, device_key)
                return
            entity_id, action = resolved
            command_name = next(
                (c["name"] for c in device.get("commands", []) if c.get("value") == value),
                value,
            )
        state = self.hass.states.get(entity_id)
        attributes = dict(state.attributes) if state else {}
        platform = next(
            (e.platform for e in self.entities() + [
                m for g in self.groups() for m in g.entities
            ] if e.entity_id == entity_id),
            None,
        )
        call = service_call(entity_id, action, attributes, platform)
        if call is None:
            _LOGGER.warning("Ignoring %s for %s: no service for it", value, entity_id)
            return
        domain, service, data = call
        _LOGGER.debug("Hub sent %s for %s: %s.%s", value, entity_id, domain, service)
        self.hass.bus.async_fire(
            "melodious.command_received",
            {
                "hub": self.entry.data.get("host"),
                "device": device.get("name") if device else device_key,
                "device_key": device.get("key", device.get("entity")) if device else device_key,
                "entity_id": entity_id,
                "command": command_name,
                "action": action,
                "value": value,
            },
        )
        self.hass.async_create_task(self.hass.services.async_call(domain, service, data, blocking=False))


async def async_setup_entry(hass: HomeAssistant, entry: MelodiousConfigEntry) -> bool:
    runtime = MelodiousRuntime(hass, entry)
    await runtime.async_load()
    entry.runtime_data = runtime

    async def handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            runtime.run(str(body.get("device", "")), str(body.get("command", "")))
        # Always 200: any other answer makes the hub's engine rescan the network.
        return web.Response(status=200)

    webhook.async_register(
        hass, DOMAIN, entry.title, entry.data[CONF_WEBHOOK_ID], handle_webhook,
        local_only=True, allowed_methods=["POST"],
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID]))
    entry.async_on_unload(runtime._cancel_timer)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    tracked = set(entry.options.get(CONF_ENTITIES, []))
    for group in runtime.groups():
        tracked.update(member.entity_id for member in group.entities)
    entry.async_on_unload(
        async_track_state_change_event(hass, sorted(tracked), runtime.async_state_changed)
    )

    async def push_when_started(_hass: HomeAssistant) -> None:
        # Once per start: the hub may have been reset since the last push.
        await runtime.async_push(force=True)

    entry.async_on_unload(async_at_started(hass, push_when_started))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: MelodiousConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MelodiousConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget this entry, but leave the hub's devices alone.

    Removing the integration is often the first step of re-adding it; wiping
    the hub here would strand every activity and button that uses a device.
    Unticking a device in Configure is how one comes off the hub.
    """
    await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}").async_remove()
