"""HA 2026.1.2 runtime tests for the mirror Configure step and button entity."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.melodious.catalogue import Catalogue
from custom_components.melodious.button import MirrorCommandButton
from custom_components.melodious.config_flow import MelodiousOptionsFlow
from custom_components.melodious.const import CONF_MIRROR_DEVICES
from custom_components.melodious.mirror import MirrorButtonSpec

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "tests/fixtures/hub_activity_config.json").read_text())
NAKAMICHI = "81897247"
HA_LIGHT = "91684061"
HOST = "hub.example"


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


class MirrorFlowTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, options=None):
        flow = MelodiousOptionsFlow()
        flow._pending_options = {}
        runtime = SimpleNamespace(
            client=SimpleNamespace(activity_config=AsyncMock(return_value=CONFIG))
        )
        config_entry = entry(options=options or {}, runtime_data=runtime)
        return flow, config_entry

    async def test_picker_lists_hub_only_devices(self):
        flow, config_entry = self.flow()
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
                lambda _: config_entry)):
            result = await flow.async_step_mirror()
        self.assertEqual(result["step_id"], "mirror")
        self.assertIn(NAKAMICHI, flow._mirror_device_ids)
        self.assertNotIn(HA_LIGHT, flow._mirror_device_ids)
        self.assertEqual(len(flow._mirror_device_ids), 5)

    async def test_saved_devices_are_the_defaults(self):
        flow, config_entry = self.flow(options={
            CONF_MIRROR_DEVICES: [{"device_id": NAKAMICHI, "disabled_commands": []}]
        })
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
                lambda _: config_entry)):
            await flow.async_step_mirror()
        self.assertEqual(flow._mirror_device_ids, {
            "66690268", "74521691", NAKAMICHI, "91684058", "91684059",
        })

    async def test_review_saves_disabled_commands_and_preserves_other_options(self):
        flow, config_entry = self.flow(options={"entities": ["light.lamp"],
                                                "theater_links": [{"port_id": "HDMI_1"}]})
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
                lambda _: config_entry)):
            await flow.async_step_mirror()
            review = await flow.async_step_mirror({CONF_MIRROR_DEVICES: [NAKAMICHI]})
            self.assertEqual(review["step_id"], "mirror_review")
            with patch.object(flow, "async_create_entry", side_effect=lambda **kw: kw):
                saved = await flow.async_step_mirror_review({
                    f"disabled_{NAKAMICHI}": ["VolumeUp", "NotACommand"],
                })
        data = saved["data"]
        self.assertEqual(data["entities"], ["light.lamp"])
        self.assertEqual(data["theater_links"], [{"port_id": "HDMI_1"}])
        self.assertEqual(len(data[CONF_MIRROR_DEVICES]), 1)
        device = data[CONF_MIRROR_DEVICES][0]
        self.assertEqual(device["device_id"], NAKAMICHI)
        self.assertEqual(device["manufacturer"], "Nakamichi")
        self.assertEqual(device["model"], "Shockwafe Pro 7.1")
        self.assertEqual(device["disabled_commands"], ["VolumeUp"])

    async def test_finish_keeps_pending_mirror_options(self):
        flow = MelodiousOptionsFlow()
        flow._pending_options = {
            CONF_MIRROR_DEVICES: [{"device_id": NAKAMICHI, "disabled_commands": []}],
        }
        flow._edit_mirror_devices = False
        flow._edit_theater_links = True
        flow._theater_done = True
        config_entry = entry()
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
                lambda _: config_entry)), \
                patch.object(flow, "async_create_entry", side_effect=lambda **kw: kw):
            saved = await flow._finish({"theater_links": []})
        self.assertEqual(saved["data"][CONF_MIRROR_DEVICES],
                         [{"device_id": NAKAMICHI, "disabled_commands": []}])
        self.assertEqual(saved["data"]["theater_links"], [])

    async def test_a_home_assistant_device_cannot_be_mirrored(self):
        flow, config_entry = self.flow()
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
                lambda _: config_entry)):
            await flow.async_step_mirror()
            with patch.object(flow, "async_create_entry", side_effect=lambda **kw: kw):
                saved = await flow.async_step_mirror({CONF_MIRROR_DEVICES: [HA_LIGHT]})
        self.assertEqual(saved["data"][CONF_MIRROR_DEVICES], [])


class MirrorButtonEntityTests(unittest.IsolatedAsyncioTestCase):
    def test_entity_uses_the_hubs_manufacturer_and_model(self):
        catalogue = Catalogue.from_config(CONFIG)
        device = catalogue.device_by_id(NAKAMICHI)
        spec = MirrorButtonSpec(device, "VolumeUp")
        coordinator = SimpleNamespace(
            data=(catalogue, None), catalogue=catalogue, client=Mock(),
            last_update_success=True,
        )
        config_entry = entry()
        entity = MirrorCommandButton(coordinator, config_entry, spec)
        self.assertEqual(entity.unique_id, f"{HOST}_{NAKAMICHI}_button_VolumeUp")
        self.assertEqual(entity.name, "VolumeUp")
        self.assertEqual(entity.device_info["manufacturer"], "Nakamichi")
        self.assertEqual(entity.device_info["model"], "Shockwafe Pro 7.1")
        self.assertIn((f"{HOST}:{NAKAMICHI}"), str(entity.device_info["identifiers"]))
        self.assertTrue(entity.extra_state_attributes["tap_only"])
        self.assertIn("hold_secs", entity.extra_state_attributes["hold"])

    async def test_press_sends_the_hub_command(self):
        catalogue = Catalogue.from_config(CONFIG)
        device = catalogue.device_by_id(NAKAMICHI)
        client = SimpleNamespace(send_command=AsyncMock(return_value={"reply": '{"code":200}'}))
        coordinator = SimpleNamespace(
            data=(catalogue, None), catalogue=catalogue, client=client,
            last_update_success=True,
        )
        entity = MirrorCommandButton(
            coordinator, entry(), MirrorButtonSpec(device, "VolumeUp")
        )
        await entity.async_press()
        client.send_command.assert_awaited_once_with(NAKAMICHI, "VolumeUp")


if __name__ == "__main__":
    unittest.main()
