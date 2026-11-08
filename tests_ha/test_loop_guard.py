"""HA 2026.1.2 runtime tests for the HA->hub loop guard.

The guard keeps entities Melodious created (mirrored hub devices and the hub's
own remote/activity entities) out of the Configure pickers that push Home
Assistant devices back to the same hub.
"""

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from custom_components.melodious.config_flow import MelodiousOptionsFlow
from custom_components.melodious.const import CONF_DEVICES, CONF_ENTITIES

HOST = "hub.example"
MIRROR_DEVICE = "mirror-device"
REAL_DEVICE = "real-device"
MIXED_DEVICE = "mixed-device"

OWN_ENTITIES = [
    "button.amp_volume_up",
    "switch.amp_power",
    "select.amp_input",
    "sensor.amp_activity",
    "remote.hub",
    "select.hub_activity",
    "sensor.hub_activity",
]
DEVICE_ENTITIES = {
    MIRROR_DEVICE: OWN_ENTITIES[:4],
    REAL_DEVICE: ["media_player.lg_tv"],
    MIXED_DEVICE: ["media_player.lg_tv", "button.amp_volume_up"],
}
DEVICE_NAMES = {
    MIRROR_DEVICE: "Nakamichi Amp",
    REAL_DEVICE: "LG TV",
    MIXED_DEVICE: "Mixed device",
}


def entry(**overrides):
    values = {
        "entry_id": "entry1",
        "unique_id": HOST,
        "title": "Melodious hub",
        "data": {"host": HOST, "port": 8080},
        "options": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _entry(entity_id, platform="melodious"):
    return SimpleNamespace(entity_id=entity_id, platform=platform)


def _entries_for_device(_registry, device_id, include_disabled_entities=False):
    return [_entry(entity_id) for entity_id in DEVICE_ENTITIES.get(device_id, [])]


def _device(device_id):
    return SimpleNamespace(
        name_by_user=None,
        name=DEVICE_NAMES.get(device_id, device_id),
        manufacturer=None,
        model=None,
    )


class LoopGuardTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, options=None):
        flow = MelodiousOptionsFlow()
        flow.hass = SimpleNamespace(states=Mock(get=lambda _: None))
        config_entry = entry(options=options or {})
        return flow, config_entry

    @contextmanager
    def registries(self, config_entry, own=None):
        own = OWN_ENTITIES if own is None else own
        ent_reg = Mock()
        dev_reg = SimpleNamespace(async_get=_device)
        with patch.object(
            MelodiousOptionsFlow,
            "config_entry",
            new_callable=lambda: property(lambda _: config_entry),
        ), patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=ent_reg,
        ), patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
            return_value=[_entry(entity_id) for entity_id in own],
        ), patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            side_effect=_entries_for_device,
        ), patch(
            "homeassistant.helpers.device_registry.async_get",
            return_value=dev_reg,
        ):
            yield

    async def test_entity_picker_excludes_the_integrations_own_entities(self):
        flow, config_entry = self.flow()
        with self.registries(config_entry):
            result = await flow.async_step_init()
        self.assertEqual(result["step_id"], "init")
        entity_selector = next(
            selector
            for marker, selector in result["data_schema"].schema.items()
            if str(marker) == CONF_ENTITIES
        )
        self.assertEqual(
            entity_selector.config["exclude_entities"], OWN_ENTITIES
        )

    async def test_mirror_only_device_is_rejected_with_its_name(self):
        flow, config_entry = self.flow()
        with self.registries(config_entry):
            result = await flow.async_step_init({
                CONF_DEVICES: [MIRROR_DEVICE],
                CONF_ENTITIES: [],
            })
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(result["errors"], {"base": "mirror_loop"})
        self.assertIn(
            "Nakamichi Amp", result["description_placeholders"]["devices"]
        )

    async def test_mirrored_entity_bypassing_the_picker_is_rejected(self):
        flow, config_entry = self.flow()
        with self.registries(config_entry):
            result = await flow.async_step_init({
                CONF_DEVICES: [],
                CONF_ENTITIES: [OWN_ENTITIES[0]],
            })
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(result["errors"], {"base": "mirror_loop"})
        self.assertIn(
            OWN_ENTITIES[0], result["description_placeholders"]["devices"]
        )

    async def test_finish_rejects_own_entities_from_a_direct_options_write(self):
        flow, config_entry = self.flow()
        flow._pending_options = {
            CONF_DEVICES: [],
            CONF_ENTITIES: [OWN_ENTITIES[0]],
        }
        flow._edit_mirror_devices = False
        flow._edit_theater_links = False
        with self.registries(config_entry):
            result = await flow._finish({})
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(result["errors"], {"base": "mirror_loop"})
        self.assertIn(
            OWN_ENTITIES[0], result["description_placeholders"]["devices"]
        )

    async def test_finish_rejects_an_own_entity_selected_from_a_mixed_device(self):
        flow, config_entry = self.flow()
        flow._pending_options = {
            CONF_DEVICES: [{
                "device_id": MIXED_DEVICE,
                "entities": [OWN_ENTITIES[0]],
                "disabled_commands": [],
            }],
            CONF_ENTITIES: [],
        }
        flow._edit_mirror_devices = False
        flow._edit_theater_links = False
        with self.registries(config_entry):
            result = await flow._finish({})
        self.assertEqual(result["step_id"], "init")
        self.assertEqual(result["errors"], {"base": "mirror_loop"})
        self.assertIn(
            "Mixed device", result["description_placeholders"]["devices"]
        )

    async def test_finish_accepts_a_mixed_device_with_real_entities_only(self):
        flow, config_entry = self.flow()
        flow._pending_options = {
            CONF_DEVICES: [{
                "device_id": MIXED_DEVICE,
                "entities": ["media_player.lg_tv"],
                "disabled_commands": [],
            }],
            CONF_ENTITIES: [],
        }
        flow._edit_mirror_devices = False
        flow._edit_theater_links = False
        with self.registries(config_entry), patch.object(
            flow, "async_create_entry", side_effect=lambda **kw: kw
        ):
            result = await flow._finish({})
        self.assertEqual(result["data"][CONF_DEVICES][0]["device_id"], MIXED_DEVICE)

    async def test_real_ha_device_with_a_mirrored_counterpart_is_accepted(self):
        flow, config_entry = self.flow()
        flow.async_step_review = AsyncMock(return_value={"step_id": "review"})
        with self.registries(config_entry):
            result = await flow.async_step_init({
                CONF_DEVICES: [REAL_DEVICE],
                CONF_ENTITIES: [],
            })
        self.assertEqual(result, {"step_id": "review"})
        flow.async_step_review.assert_awaited_once()

    async def test_a_device_with_any_real_entity_is_accepted(self):
        flow, config_entry = self.flow()
        flow.async_step_review = AsyncMock(return_value={"step_id": "review"})
        with self.registries(config_entry):
            result = await flow.async_step_init({
                CONF_DEVICES: [MIXED_DEVICE],
                CONF_ENTITIES: [],
            })
        self.assertEqual(result, {"step_id": "review"})
        flow.async_step_review.assert_awaited_once()

    async def test_review_step_rejects_a_mirror_only_device(self):
        flow, config_entry = self.flow()
        flow._picked_devices = [MIRROR_DEVICE]
        flow._picked_entities = []
        with self.registries(config_entry):
            result = await flow.async_step_review({
                f"entities_{MIRROR_DEVICE}": OWN_ENTITIES[:4],
                f"disabled_{MIRROR_DEVICE}": [],
            })
        self.assertEqual(result["step_id"], "review")
        self.assertEqual(result["errors"], {"base": "mirror_loop"})
        self.assertIn(
            "Nakamichi Amp", result["description_placeholders"]["devices"]
        )


if __name__ == "__main__":
    unittest.main()
