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
