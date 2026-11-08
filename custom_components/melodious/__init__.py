"""Melodious: Home Assistant entities as devices on a Melodious Harmony hub.

Each exposed entity becomes an ordinary Harmony device on the hub, so it can
sit in activities and on remote buttons. Its commands are IP commands: the
hub's engine posts {"device": <entity_id>, "command": <value>} to this
integration's webhook, which runs the matching service call. No Home
Assistant token lives on the hub; the webhook ID is the only secret.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from aiohttp import web

from homeassistant.components import persistent_notification, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, STATE_UNAVAILABLE, Platform
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import (
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_HA_URL,
    CONF_THEATER_LINKS,
    CONF_WEBHOOK_ID,
    DOMAIN,
    PUSH_DELAY_SECONDS,
    REFUSED_RETRY_SECONDS,
    RETRY_SECONDS,
    STATE_DEBOUNCE_SECONDS,
    STATE_HEARTBEAT_SECONDS,
)
from .devices import (
    DeviceGroup,
    Entity,
    allowed_values,
    build_device_set,
    build_merged_set,
    build_state_payload,
    resolve_v2_command,
    service_call,
    skip_reason,
)
from .hub import HubClient, HubError, HubRefused
from .webos import POLL_SECONDS, WebOSObservations

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
        # The set the hub last refused: re-sending it gets the same answer.
        self._refused: dict | None = None
        self._refused_at = 0.0
        self._hub_seed: dict | None = None
        self.allowed: dict[str, set[str]] = {}
        self.taken_names: set[str] = set()
        self._lock = asyncio.Lock()
        self._timer: CALLBACK_TYPE | None = None
        # Volatile state push (separate lock/timer from the device-set writer).
        self._state_lock = asyncio.Lock()
        self._state_timer: CALLBACK_TYPE | None = None
        self._state_seq = 0
        self._last_state_push = 0.0
        self._ha_state_supported: bool | None = None
        self._ha_state_skip_logged = False
        self.webos = WebOSObservations(self._read_webos, self._webos_is_on)

    def _webos_is_on(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state in ("on", "idle", "playing", "paused", "buffering")

    async def _read_webos(self, entity_id: str, uri: str) -> dict:
        return await self.hass.services.async_call(
            "webostv", "command", {"entity_id": entity_id, "command": uri},
            blocking=True, return_response=True,
        )

    def _with_webos(self, entity: Entity) -> Entity:
        if entity.platform == "webostv":
            entity.webos_inputs = self.webos.ports.get(entity.entity_id)
            entity.webos_theater = self.webos.theater(entity.entity_id)
        return entity

    async def async_refresh_webos(self) -> None:
        selected = self.entities() + [m for g in self.groups() for m in g.entities]
        await self.webos.refresh([e.entity_id for e in selected
                                  if e.platform == "webostv" and e.domain == "media_player"])
        self.schedule_state_push(immediate=True)
        self._schedule_push_if_changed()

    @callback
    def _webos_poll_fired(self, _now: Any) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_refresh_webos(), f"{DOMAIN} webOS observations",
        )

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
                                 device_id=device_id, platform=platform,
                                 state=STATE_UNAVAILABLE))
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
                    state=state.state,
                )
            )
        return [self._with_webos(e) for e in out]

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
                    state=state.state if state else STATE_UNAVAILABLE,
                ))
            groups.append(DeviceGroup(device_id, name or device_id, manufacturer, model,
                                      [self._with_webos(e) for e in members]))
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

    def _refused_recently(self, device_set: dict) -> bool:
        return device_set == self._refused and \
            time.monotonic() - self._refused_at < REFUSED_RETRY_SECONDS

    @callback
    def _schedule_push_if_changed(self) -> None:
        if self._timer is not None or self.sent is None:
            return
        device_set = self._build()
        if device_set != self.sent and not self._refused_recently(device_set):
            self._timer = async_call_later(self.hass, PUSH_DELAY_SECONDS, self._timer_fired)

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
                try:
                    features = await self.client.features()
                    self._ha_state_supported = "ha-state" in features
                except HubError:
                    features = {"ha-devices-v2"} if groups else set()
                if groups:
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
                if not force and (device_set == self.sent or self._refused_recently(device_set)):
                    return
                result = await self.client.commit(device_set)
            except HubRefused as err:
                _LOGGER.warning("The hub refused the Home Assistant devices: %s", err)
                self._refused = device_set
                self._refused_at = time.monotonic()
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
            self._refused = None
            self.allowed = allowed_values(device_set)
            await self.store.async_save(device_set)
            persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_{self.entry.entry_id}")
            if result.get("changed"):
                _LOGGER.info(
                    "Hub devices: added %s, updated %s, removed %s",
                    result.get("added") or [], result.get("updated") or [], result.get("removed") or [],
                )
        self.schedule_state_push(immediate=True)

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
    def _cancel_state_timer(self) -> None:
        if self._state_timer:
            self._state_timer()
            self._state_timer = None

    @callback
    def schedule_state_push(self, immediate: bool = False) -> None:
        """Push now if the last push was long enough ago, else once the interval is up."""
        if self._state_timer is not None or self.sent is None:
            return
        now = time.monotonic()
        elapsed = now - self._last_state_push
        if immediate and elapsed >= STATE_DEBOUNCE_SECONDS:
            self.entry.async_create_background_task(
                self.hass, self.async_push_state(), f"{DOMAIN} state push"
            )
            return
        delay = STATE_DEBOUNCE_SECONDS if not immediate else max(0.1, STATE_DEBOUNCE_SECONDS - elapsed)
        if elapsed < STATE_DEBOUNCE_SECONDS:
            delay = max(delay, STATE_DEBOUNCE_SECONDS - elapsed)
        self._state_timer = async_call_later(self.hass, delay, self._state_timer_fired)

    @callback
    def _state_timer_fired(self, _now: Any) -> None:
        self._state_timer = None
        self.entry.async_create_background_task(
            self.hass, self.async_push_state(), f"{DOMAIN} state push"
        )

    @callback
    def _heartbeat_fired(self, _now: Any) -> None:
        self.schedule_state_push(immediate=True)

    async def async_push_state(self) -> None:
        """Push one batched volatile state snapshot for all exposed devices."""
        if self.sent is None:
            return
        async with self._state_lock:
            now = time.monotonic()
            if (now - self._last_state_push) < STATE_DEBOUNCE_SECONDS:
                self.schedule_state_push(immediate=False)
                return
            if self._ha_state_supported is None:
                features = await self.client.features()
                self._ha_state_supported = "ha-state" in features
            if not self._ha_state_supported:
                if not self._ha_state_skip_logged:
                    self._ha_state_skip_logged = True
                    _LOGGER.debug("Hub does not advertise ha-state in /api/version; skipping state pushes")
                return
            self._state_seq += 1
            payload = build_state_payload(
                self.groups(), self.entities(), self.sent, self._state_seq, int(time.time()),
                self.entry.options.get(CONF_THEATER_LINKS),
            )
            self._last_state_push = time.monotonic()
            try:
                await self.client.push_state(payload)
            except HubError as err:
                _LOGGER.debug("Could not push Home Assistant state to the hub: %s", err)

    @callback
    def async_state_changed(self, _event: Event[EventStateChangedData]) -> None:
        """Push the device set only when it would change; push state straight away."""
        entity_id = _event.data.get("entity_id")
        if entity_id in self.webos.ports or entity_id in self.webos.live:
            self.webos.invalidate(entity_id)
            # HA's source/state event refreshes observations immediately; the
            # interval also catches changes not represented in HA attributes.
            self._webos_poll_fired(None)
        self.schedule_state_push(immediate=True)
        self._schedule_push_if_changed()

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
        if device is not None:
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
        # The event above always fires (the hub did ask); only the service
        # call skips when fresh state already matches (§5.5). theater() is
        # None unless the webOS observation is fresh and the TV is on.
        reason = skip_reason(
            action,
            state.state if state else None,
            state is not None and state.state != STATE_UNAVAILABLE,
            attributes,
            self.webos.theater(entity_id) if platform == "webostv" else None,
        )
        if reason:
            _LOGGER.debug("Skipping %s for %s: %s", value, entity_id, reason)
            return
        async def execute_service() -> None:
            is_input = action.startswith("webos_input:")
            await self.hass.services.async_call(domain, service, data, blocking=is_input)
            if is_input:
                self.webos.invalidate(entity_id)
                await self.async_refresh_webos()

        self.entry.async_create_background_task(self.hass, execute_service(), f"{DOMAIN} command")


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
    entry.async_on_unload(runtime._cancel_state_timer)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    tracked = set(entry.options.get(CONF_ENTITIES, []))
    for group in runtime.groups():
        tracked.update(member.entity_id for member in group.entities)
    entry.async_on_unload(
        async_track_state_change_event(hass, sorted(tracked), runtime.async_state_changed)
    )
    entry.async_on_unload(
        async_track_time_interval(
            hass, runtime._heartbeat_fired, timedelta(seconds=STATE_HEARTBEAT_SECONDS)
        )
    )
    entry.async_on_unload(async_track_time_interval(
        hass, runtime._webos_poll_fired, timedelta(seconds=POLL_SECONDS),
    ))

    async def push_when_started(_hass: HomeAssistant) -> None:
        # Once per start: the hub may have been reset since the last push.
        await runtime.async_refresh_webos()
        await runtime.async_push(force=True)
        runtime.schedule_state_push(immediate=True)

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
