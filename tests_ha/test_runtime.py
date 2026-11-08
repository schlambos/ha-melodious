"""Version-pinned runtime tests; run with HA 2026.1.2, Python 3.13.

uv run --python 3.13 --with homeassistant==2026.1.2 python -m unittest discover -s tests_ha
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.melodious import MelodiousRuntime
from custom_components.melodious.config_flow import MelodiousOptionsFlow
from custom_components.melodious.const import REFUSED_RETRY_SECONDS
from custom_components.melodious.hub import HubRefused
from custom_components.melodious.devices import DeviceGroup, Entity
from custom_components.melodious.webos import INPUTS, VOLUME, FOREGROUND

ENTITY = "media_player.tv"
CAPTURE = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/lg_external_inputs.json").read_text())


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tasks = []
        def task(hass, coro, name):
            result = asyncio.create_task(coro)
            self.tasks.append(result)
            return result
        self.entry = SimpleNamespace(data={}, options={}, entry_id="test", async_create_background_task=task)
        self.hass = SimpleNamespace(states=Mock(), services=SimpleNamespace(async_call=AsyncMock()), bus=Mock())
        self.hass.states.get.return_value = SimpleNamespace(state="on", attributes={})
        with patch("custom_components.melodious.hub_client"), patch("custom_components.melodious.Store"):
            self.runtime = MelodiousRuntime(self.hass, self.entry)
        self.runtime.entities = lambda: [Entity(ENTITY, "LG", platform="webostv", state="on")]
        self.runtime.groups = lambda: []

    async def asyncTearDown(self):
        self.runtime._cancel_timer()
        self.runtime._cancel_state_timer()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_reads_use_response_capable_ha_service_without_controls(self):
        responses = {INPUTS: CAPTURE, VOLUME: {"volumeStatus": {"volume": 14}},
                     FOREGROUND: {"appId": "com.webos.app.hdmi1"}}
        async def service(domain, service, data, **kwargs):
            self.assertEqual((domain, service), ("webostv", "command"))
            self.assertEqual(kwargs, {"blocking": True, "return_response": True})
            self.assertNotIn("payload", data)
            return {ENTITY: {"returnValue": True, **responses[data["command"]]}}
        self.hass.services.async_call.side_effect = service
        await self.runtime.async_refresh_webos()
        self.assertEqual(self.hass.services.async_call.await_count, 3)
        enriched = self.runtime._with_webos(self.runtime.entities()[0])
        self.assertEqual(len(enriched.webos_inputs), 4)
        self.assertEqual(enriched.webos_theater["active_input"]["id"], "HDMI_1")

    async def test_off_never_calls_webostv(self):
        self.hass.states.get.return_value.state = "off"
        await self.runtime.async_refresh_webos()
        self.hass.services.async_call.assert_not_awaited()

    async def test_webhook_is_immediate_and_refreshes_after_input_service_completes(self):
        value = f"{ENTITY}:webos_input:HDMI_1"
        self.runtime.sent = {"devices": [{"key": ENTITY, "name": "LG", "entities": [
            {"alias": ENTITY, "entity": ENTITY}], "commands": [{"name": "Input HDMI 1", "value": value}]}]}
        self.runtime.allowed = {ENTITY: {value}}
        refreshed = []
        async def refresh():
            self.assertEqual(self.hass.services.async_call.await_count, 1)
            refreshed.append(True)
        self.runtime.async_refresh_webos = refresh
        self.runtime.run(ENTITY, value)
        self.hass.services.async_call.assert_not_awaited()
        await asyncio.gather(*self.tasks)
        self.hass.services.async_call.assert_awaited_once_with("webostv", "command", {
            "entity_id": ENTITY, "command": "tv/switchInput", "payload": {"inputId": "HDMI_1"}}, blocking=True)
        self.assertEqual(refreshed, [True])
        self.assertEqual(self.hass.bus.async_fire.call_args.args[1]["command"], "Input HDMI 1")

    async def test_refused_set_is_not_sent_again_until_it_changes(self):
        # A refusal is the hub's answer to that exact set; every re-send cost
        # the hub a flash backup and stalled its engine (remote presses timed out).
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        client = self.runtime.client
        client.device_names = AsyncMock(return_value=set())
        client.features = AsyncMock(return_value={"ha-devices-v2"})
        client.commit = AsyncMock(side_effect=HubRefused("activities still use inputs"))
        self.runtime.sent = {"webhook": "http://ha/api/webhook/hook", "devices": []}
        self.runtime.groups = lambda: []
        self.runtime.entities = lambda: [Entity("light.lamp", "Lamp", state="on")]
        with patch("custom_components.melodious.persistent_notification"), \
                patch("custom_components.melodious.async_call_later") as later:
            self.runtime._schedule_push_if_changed()
            self.assertEqual(later.call_count, 1)
            self.runtime._timer = None
            await self.runtime.async_push()
            self.assertEqual(client.commit.await_count, 1)
            for _ in range(5):
                self.runtime._schedule_push_if_changed()
                await self.runtime.async_push()
            self.assertEqual(later.call_count, 1)
            self.assertEqual(client.commit.await_count, 1)
            # A different set goes out at once.
            self.runtime.entities = lambda: [Entity("light.lamp", "Lamp", state="on"),
                                             Entity("light.desk", "Desk", state="on")]
            self.runtime._schedule_push_if_changed()
            self.assertEqual(later.call_count, 2)
            # So does the refused one, once the retry window is up.
            self.runtime._timer = None
            self.runtime.entities = lambda: [Entity("light.lamp", "Lamp", state="on")]
            self.runtime._refused_at -= REFUSED_RETRY_SECONDS
            self.runtime._schedule_push_if_changed()
            self.assertEqual(later.call_count, 3)
            await self.runtime.async_push(force=True)
            self.assertEqual(client.commit.await_count, 2)


class OptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_configuration_preserves_links(self):
        flow = MelodiousOptionsFlow()
        links = [{"entity_id": ENTITY, "port_id": "HDMI_2", "hub_device_id": "123"}]
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
            lambda _: SimpleNamespace(options={"theater_links": links}))), \
                patch.object(flow, "async_create_entry", side_effect=lambda **kw: kw):
            result = await flow._finish({"entities": []})
        self.assertEqual(result["data"]["theater_links"], links)

    async def test_explicit_links_can_be_saved_and_cleared_without_controls(self):
        flow = MelodiousOptionsFlow()
        flow._pending_options = {"entities": [ENTITY]}
        flow._link_fields = {"HDMI 2": (ENTITY, "HDMI_2")}
        flow._link_device_ids = {"123", "456"}
        runtime = SimpleNamespace(client=Mock())
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
            lambda _: SimpleNamespace(runtime_data=runtime))), \
                patch.object(flow, "async_create_entry", side_effect=lambda **kw: kw):
            saved = await flow.async_step_theater({"HDMI 2": ["123", "456"]})
            cleared = await flow.async_step_theater({"HDMI 2": []})
        self.assertEqual(len(saved["data"]["theater_links"]), 2)
        self.assertEqual(cleared["data"]["theater_links"], [])
        runtime.client.assert_not_called()

    async def test_review_step_can_open_theater_links_and_keeps_picks(self):
        flow = MelodiousOptionsFlow()
        flow._picked_devices = ["dev1"]
        flow._picked_entities = ["light.lamp"]
        flow.hass = SimpleNamespace(states=Mock(get=lambda _: SimpleNamespace(
            name="TV", state="on", attributes={"supported_features": 128})))
        device = SimpleNamespace(name_by_user="TV", name="TV", manufacturer=None, model=None)
        entry = SimpleNamespace(entity_id=ENTITY, platform="webostv")
        with patch.object(MelodiousOptionsFlow, "config_entry", new_callable=lambda: property(
            lambda _: SimpleNamespace(options={"theater_links": []}, runtime_data=None))), \
                patch("homeassistant.helpers.device_registry.async_get", return_value=SimpleNamespace(
                    async_get=lambda _: device)), \
                patch("homeassistant.helpers.entity_registry.async_get", return_value=Mock()), \
                patch("homeassistant.helpers.entity_registry.async_entries_for_device", return_value=[entry]), \
                patch.object(flow, "async_step_theater", AsyncMock(return_value={"step_id": "theater"})) as theater:
            result = await flow.async_step_review({"entities_dev1": [ENTITY], "disabled_dev1": [],
                                                   "edit_theater_links": True})
        theater.assert_awaited_once()
        self.assertEqual(result, {"step_id": "theater"})
        self.assertEqual(flow._pending_options["entities"], ["light.lamp"])
        self.assertEqual(flow._pending_options["devices"][0]["device_id"], "dev1")
