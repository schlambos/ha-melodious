"""Mirrored devices link to the hub device through Home Assistant's real registries.

Home Assistant 2026.11 deprecates DeviceInfo's via_device (removed in 2027.8)
for via_device_id, and reports every entity that still sets it (#202). Older
releases only know via_device. These tests add the integration's entities
through a real EntityPlatform and device registry on whichever release is
installed:

uv run --python 3.13 --with homeassistant==2026.1.2 python -m unittest discover -s tests_ha
uv run --python 3.14 --with homeassistant==2026.11.1 python -m unittest discover -s tests_ha
"""

from datetime import timedelta
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant import config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.melodious import MelodiousRuntime
from custom_components.melodious.button import MirrorButtonSpec, MirrorCommandButton
from custom_components.melodious.catalogue import Catalogue
from custom_components.melodious.const import DOMAIN
from custom_components.melodious.entity import hub_device_info, hub_key
from custom_components.melodious.select import MirrorInputSelect
from custom_components.melodious.sensor import MirrorActivitySensor
from custom_components.melodious.switch import MirrorPowerSwitch

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "tests/fixtures/hub_activity_config.json").read_text())
HOST = "hub.example"
LG = "74521691"
NAKAMICHI = "81897247"


def _config_entry():
    fields = dict(
        version=1, minor_version=1, domain=DOMAIN, title="Melodious hub",
        data={"host": HOST, "port": 8080}, source="user", options={},
        unique_id=HOST, discovery_keys={}, subentries_data=None,
    )
    # Keep only the fields this release's ConfigEntry takes.
    accepted = config_entries.ConfigEntry.__init__.__code__.co_varnames
    return config_entries.ConfigEntry(**{k: v for k, v in fields.items() if k in accepted})


class DeviceLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self._dir.name)
        loader.async_setup(self.hass)
        if hasattr(frame, "async_setup"):
            frame.async_setup(self.hass)
        self.hass.config_entries = config_entries.ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        for registry in (dr, er):
            if hasattr(registry, "async_setup"):
                registry.async_setup(self.hass)
            await registry.async_load(self.hass)
        self.entry = _config_entry()
        self.hass.config_entries._entries[self.entry.entry_id] = self.entry
        self.registry = dr.async_get(self.hass)

        # What async_setup_entry does before forwarding the platforms.
        with patch("custom_components.melodious.hub_client"), patch("custom_components.melodious.Store"):
            runtime = MelodiousRuntime(self.hass, self.entry)
        info = hub_device_info(self.entry)
        hub = self.registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers=info["identifiers"],
            manufacturer=info.get("manufacturer"),
            model=info.get("model"),
            name=info.get("name"),
            configuration_url=info.get("configuration_url"),
        )
        runtime.hub_device_id = hub.id
        self.entry.runtime_data = runtime
        self.hub = hub

        catalogue = Catalogue.from_config(CONFIG)
        self.coordinator = SimpleNamespace(
            hass=self.hass, data=(catalogue, None), catalogue=catalogue,
            activity_id=None, client=SimpleNamespace(), last_update_success=True,
            async_request_refresh=AsyncMock(), async_add_listener=lambda *a, **k: (lambda: None),
        )
        self.catalogue = catalogue

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)
        self._dir.cleanup()

    async def _add(self, domain, entity):
        platform = EntityPlatform(
            hass=self.hass, logger=logging.getLogger(__name__), domain=domain,
            platform_name=DOMAIN, platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None,
        )
        platform.config_entry = self.entry
        await platform.async_add_entities([entity])
        return entity

    def _entities(self):
        lg = self.catalogue.device_by_id(LG)
        amp = self.catalogue.device_by_id(NAKAMICHI)
        return [
            ("switch", MirrorPowerSwitch(self.coordinator, self.entry, lg)),
            ("select", MirrorInputSelect(self.coordinator, self.entry, lg)),
            ("sensor", MirrorActivitySensor(self.coordinator, self.entry, amp)),
            ("button", MirrorCommandButton(self.coordinator, self.entry, MirrorButtonSpec(amp, "VolumeUp"))),
        ]

    async def test_mirrored_devices_link_to_the_hub_without_a_deprecation_report(self):
        reports = []
        real_report = getattr(dr, "report_usage", None)

        def record(what, *args, **kwargs):
            reports.append(what)
            if real_report is not None:
                return real_report(what, *args, **kwargs)

        with patch.object(dr, "report_usage", record, create=True):
            for domain, entity in self._entities():
                await self._add(domain, entity)

        self.assertEqual([r for r in reports if "via_device" in r], [])
        # async_get_device is itself deprecated in 2026.11, so look devices up
        # through the config entry, which every release supports.
        devices = dr.async_entries_for_config_entry(self.registry, self.entry.entry_id)
        for _domain, entity in self._entities():
            identifiers = entity.device_info["identifiers"]
            device = next((d for d in devices if identifiers <= d.identifiers), None)
            self.assertIsNotNone(device, identifiers)
            self.assertEqual(device.via_device_id, self.hub.id, device.name)

    def test_device_info_sets_only_the_link_this_release_accepts(self):
        deprecated = getattr(dr, "_DEPRECATED_DEVICE_INFO_PARAMETERS", {})
        for _domain, entity in self._entities():
            info = entity.device_info
            if "via_device_id" in getattr(dr.DeviceInfo, "__optional_keys__", ()):
                self.assertEqual(info["via_device_id"], self.hub.id)
                self.assertNotIn("via_device", info)
                self.assertEqual([key for key in deprecated if key in info], [])
            else:
                self.assertEqual(info["via_device"], (DOMAIN, hub_key(self.entry)))
                self.assertNotIn("via_device_id", info)
