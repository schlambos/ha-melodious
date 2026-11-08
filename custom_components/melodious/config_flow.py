"""Set up Melodious: the hub, and the entities it gets as devices."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.network import NoURLAvailableError, get_url

from . import hub_client
from .const import CONF_DEVICES, CONF_ENTITIES, CONF_HA_URL, CONF_THEATER_LINKS, CONF_WEBHOOK_ID, DEFAULT_PORT, DOMAIN
from .devices import SUPPORTED_DOMAINS
from .hub import HubError, HubRefused


class MelodiousConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            data = {**user_input, CONF_HOST: user_input[CONF_HOST].strip()}
            data[CONF_HA_URL] = user_input[CONF_HA_URL].strip().rstrip("/")
            await self.async_set_unique_id(data[CONF_HOST])
            self._abort_if_unique_id_configured()
            # The hub can't verify Let's Encrypt certificates.
            if not data[CONF_HA_URL].startswith("http://"):
                errors[CONF_HA_URL] = "http_only"
            else:
                try:
                    # A dry run: proves the hub has the device writer, saves nothing.
                    await hub_client(self.hass, data).commit({"devices": []}, dry_run=True)
                except HubRefused:
                    pass  # It answered; an activity just uses a device already there.
                except HubError as err:
                    errors["base"] = "cannot_connect"
                    placeholders["error"] = str(err)
            if not errors:
                return self.async_create_entry(
                    title=f"Melodious hub {data[CONF_HOST]}",
                    data={**data, CONF_WEBHOOK_ID: webhook.async_generate_id()},
                    options={CONF_ENTITIES: []},
                )

        try:
            guess = get_url(self.hass, allow_external=False, allow_ip=True, require_ssl=False)
        except NoURLAvailableError:
            guess = ""
        defaults = user_input or {CONF_HA_URL: guess if guess.startswith("http://") else ""}
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): int,
                vol.Optional(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): str,
                vol.Optional(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_HA_URL, default=defaults.get(CONF_HA_URL, "")): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors, description_placeholders=placeholders
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MelodiousOptionsFlow()


class MelodiousOptionsFlow(OptionsFlow):
    """Pick the entities the hub gets as devices."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Remember the device pick; the review step expands it.
            self._picked_devices = user_input.get(CONF_DEVICES, [])
            self._picked_entities = user_input.get(CONF_ENTITIES, [])
            self._edit_theater_links = user_input.get("edit_theater_links", False)
            if self._picked_devices:
                return await self.async_step_review()
            return await self._finish({CONF_ENTITIES: self._picked_entities, CONF_DEVICES: []})
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DEVICES,
                    default=[d.get("device_id") for d in self.config_entry.options.get(CONF_DEVICES, [])],
                ): selector.DeviceSelector(
                    selector.DeviceSelectorConfig(multiple=True)
                ),
                vol.Optional("edit_theater_links", default=False): bool,
                vol.Optional(
                    CONF_ENTITIES, default=self.config_entry.options.get(CONF_ENTITIES, [])
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=list(SUPPORTED_DOMAINS), multiple=True)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_review(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Per device: which entities to merge and which commands to skip."""
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers.entity_registry import async_entries_for_device

        from .devices import DeviceGroup, Entity, merge_device

        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        picked = getattr(self, "_picked_devices", None)
        if picked is None:
            picked = [d.get("device_id") for d in self.config_entry.options.get(CONF_DEVICES, [])]
        entities_pick = getattr(self, "_picked_entities",
                                self.config_entry.options.get(CONF_ENTITIES, []))
        saved = {d["device_id"]: d for d in self.config_entry.options.get(CONF_DEVICES, [])}

        def _group(device_id: str, entity_ids: list[str] | None = None) -> DeviceGroup:
            device = dev_reg.async_get(device_id)
            members = []
            for reg_entry in async_entries_for_device(ent_reg, device_id):
                if entity_ids is not None and reg_entry.entity_id not in entity_ids:
                    continue
                state = self.hass.states.get(reg_entry.entity_id)
                _runtime = getattr(self.config_entry, "runtime_data", None)
                members.append((_runtime._with_webos if _runtime else (lambda e: e))(Entity(
                    reg_entry.entity_id,
                    state.name if state else reg_entry.entity_id,
                    available=False if state is None else True,
                    supported_features=int((state.attributes.get("supported_features") or 0))
                    if state else 0,
                    attributes=dict(state.attributes) if state else {},
                    device_id=device_id,
                    platform=reg_entry.platform,
                    state=state.state if state else "unavailable",
                )))
            return DeviceGroup(
                device_id,
                (device.name_by_user or device.name) if device else device_id,
                device.manufacturer if device else None,
                device.model if device else None,
                members,
            )

        if user_input is not None:
            devices = []
            for device_id in picked:
                group = _group(device_id)
                reg_ids = [e.entity_id for e in group.entities]
                entities = user_input.get(f"entities_{device_id}") or reg_ids
                preview = merge_device(group, key=entities[0] if entities else device_id)
                valid = {c["name"] for c in (preview["commands"] if preview else [])}
                stored = saved.get(device_id, {})
                devices.append({
                    "device_id": device_id,
                    # The hub key is chosen at push time (existing v1 device
                    # first); a stored key survives re-configuration.
                    "key": stored.get("key"),
                    "entities": [e for e in entities if e in reg_ids] or reg_ids,
                    "disabled_commands": [c for c in
                                          (user_input.get(f"disabled_{device_id}") or [])
                                          if c in valid],
                })
            # The review's picks ride along until the theater step saves them.
            self._pending_options = {**self.config_entry.options,
                                     CONF_DEVICES: devices, CONF_ENTITIES: entities_pick}
            if user_input.get("edit_theater_links"):
                return await self.async_step_theater()
            return await self._finish({CONF_DEVICES: devices, CONF_ENTITIES: entities_pick})

        schema_dict: dict[Any, Any] = {}
        warnings: list[str] = []
        for device_id in picked:
            group = _group(device_id)
            prev = saved.get(device_id, {})
            reg_ids = [e.entity_id for e in group.entities]
            schema_dict[vol.Optional(
                f"entities_{device_id}",
                default=prev.get("entities") or reg_ids,
            )] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=reg_ids, multiple=True))
            preview = merge_device(group, key=reg_ids[0] if reg_ids else device_id,
                                   warnings=warnings)
            command_names = [c["name"] for c in (preview["commands"] if preview else [])]
            schema_dict[vol.Optional(
                f"disabled_{device_id}",
                default=[c for c in (prev.get("disabled_commands") or [])
                         if c in command_names],
            )] = selector.SelectSelector(selector.SelectSelectorConfig(
                options=command_names, multiple=True))
        schema_dict[vol.Optional("edit_theater_links", default=False)] = bool
        warning_text = "\n".join(warnings)
        return self.async_show_form(
            step_id="review", data_schema=vol.Schema(schema_dict),
            description_placeholders={"warnings": f"\n\n{warning_text}" if warning_text else ""},
        )

    async def _finish(self, options: dict) -> ConfigFlowResult:
        # Unrelated device configuration must never silently delete links.
        self._pending_options = {**self.config_entry.options, **options}
        if getattr(self, "_edit_theater_links", False):
            return await self.async_step_theater()
        return self.async_create_entry(data=self._pending_options)

    async def async_step_theater(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explicit display-only HDMI-port to hub-device links; no control writes."""
        import re
        from homeassistant.helpers import entity_registry as er

        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is None:
            return self.async_show_form(step_id="theater", data_schema=vol.Schema({}), errors={"base": "cannot_connect"})
        errors = {}
        if user_input is not None and hasattr(self, "_link_fields"):
            links = []
            for field, (entity_id, port_id) in self._link_fields.items():
                for device_id in user_input.get(field) or []:
                    if device_id not in self._link_device_ids:
                        errors["base"] = "unknown_device"
                        continue
                    links.append({"entity_id": entity_id, "port_id": port_id,
                                  "hub_device_id": device_id})
            if not errors:
                return self.async_create_entry(data={**self._pending_options, CONF_THEATER_LINKS: links})
        try:
            config = await runtime.client.activity_config()
        except HubError:
            return self.async_show_form(step_id="theater", data_schema=vol.Schema({}),
                                        errors={"base": "cannot_connect"})
        entries = (config.get("deviceList") or {}).get("DevicesWithFeatures") or []
        devices = [{"value": str(item["Device"]["Id-"]), "label": str(item["Device"]["Name"])}
                   for item in entries if item.get("Device", {}).get("Id-") is not None]
        self._link_device_ids = {d["value"] for d in devices}
        self._link_fields = {}
        schema = {}
        selected = self._pending_options
        wanted = set(selected.get(CONF_ENTITIES) or [])
        for group in selected.get(CONF_DEVICES) or []:
            wanted.update(group.get("entities") or [])
        saved = selected.get(CONF_THEATER_LINKS) or []
        registry = er.async_get(self.hass)
        lg_entities = [eid for eid in wanted if (reg := registry.async_get(eid))
                       and reg.platform == "webostv" and eid.startswith("media_player.")]
        await runtime.webos.refresh(lg_entities)
        # The last hub spec supplies stable ports when the TV is off. Live
        # observations supply friendly labels, never a guessed new port list.
        for entity_id in sorted(lg_entities):
            spec = next((spec for spec in (runtime.sent or {}).get("devices", [])
                         if entity_id == spec.get("entity") or any(
                             e.get("entity") == entity_id for e in spec.get("entities") or [])), {})
            ports = {p["id"]: p["label"] for p in runtime.webos.ports.get(entity_id, [])}
            for command in spec.get("commands") or []:
                match = re.fullmatch(r"Input HDMI ([1-9]\d?)", command.get("name", ""))
                if match:
                    ports.setdefault(f"HDMI_{match[1]}", f"HDMI {match[1]}")
            for port_id, label in sorted(ports.items()):
                field = f"{entity_id} · {port_id.replace('_', ' ')} · {label}"
                self._link_fields[field] = (entity_id, port_id)
                defaults = [str(link["hub_device_id"]) for link in saved
                            if link.get("entity_id") == entity_id and link.get("port_id") == port_id
                            and str(link.get("hub_device_id")) in self._link_device_ids]
                schema[vol.Optional(field, default=defaults)] = selector.SelectSelector(
                    selector.SelectSelectorConfig(options=devices, multiple=True))
        return self.async_show_form(step_id="theater", data_schema=vol.Schema(schema), errors=errors)
