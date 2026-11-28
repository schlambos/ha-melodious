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

    def _allow(self, value, name="PowerOn"):
        self.runtime.sent = {"devices": [{"key": ENTITY, "name": "LG", "entities": [
            {"alias": ENTITY, "entity": ENTITY}], "commands": [{"name": name, "value": value}]}]}
        self.runtime.allowed = {ENTITY: {value}}

    async def test_turn_on_skipped_when_already_on_but_event_still_fires(self):
        self._allow(f"{ENTITY}:turn_on")
        self.runtime.run(ENTITY, f"{ENTITY}:turn_on")
        await asyncio.gather(*self.tasks)
        self.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.hass.bus.async_fire.call_args.args[0], "melodious.command_received")
        self.assertEqual(self.hass.bus.async_fire.call_args.args[1]["command"], "PowerOn")

    async def test_turn_on_runs_when_off_or_unavailable(self):
        self._allow(f"{ENTITY}:turn_on")
        for state in ("off", "unavailable", "unknown"):
            self.hass.states.get.return_value.state = state
            self.hass.services.async_call.reset_mock()
            self.runtime.run(ENTITY, f"{ENTITY}:turn_on")
            await asyncio.gather(*self.tasks)
            self.tasks.clear()
            self.hass.services.async_call.assert_awaited_once_with(
                "media_player", "turn_on", {"entity_id": ENTITY}, blocking=False)

    async def test_power_toggle_never_skips(self):
        self._allow(f"{ENTITY}:button:POWER", "PowerToggle")
        self.runtime.run(ENTITY, f"{ENTITY}:button:POWER")
        await asyncio.gather(*self.tasks)
        self.hass.services.async_call.assert_awaited_once_with(
            "webostv", "button", {"entity_id": ENTITY, "button": "POWER"}, blocking=False)

    async def test_webos_input_skipped_when_fresh_theater_is_on_that_port(self):
        value = f"{ENTITY}:webos_input:HDMI_1"
        self._allow(value, "Input HDMI 1")
        self.runtime.webos.theater = lambda eid: {"active_input": {"id": "HDMI_1"}}
        self.runtime.run(ENTITY, value)
        await asyncio.gather(*self.tasks)
        self.hass.services.async_call.assert_not_awaited()
        self.assertEqual(self.hass.bus.async_fire.call_args.args[1]["command"], "Input HDMI 1")
        # A different port, or no fresh observation, still switches.
        for theater in ({"active_input": {"id": "HDMI_2"}}, None):
            self.hass.services.async_call.reset_mock()
            self.runtime.webos.theater = lambda eid, t=theater: t
            self.runtime.async_refresh_webos = AsyncMock()
            self.runtime.run(ENTITY, value)
            await asyncio.gather(*self.tasks)
            self.tasks.clear()
            self.hass.services.async_call.assert_awaited_once()

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

    async def test_receiver_input_changes_do_not_recommit(self):
        # HA#27: denonavr reports transport features only on a NET input. Each
        # commit bumps the hub's configVersion and the remote syncs.
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        client = self.runtime.client
        client.device_names = AsyncMock(return_value=set())
        client.ha_devices = AsyncMock(return_value=[])
        client.features = AsyncMock(return_value={"ha-devices-v2"})
        client.commit = AsyncMock(return_value={
            "changed": True, "added": [], "updated": ["Home Theater"], "removed": []})
        self.runtime.store.async_save = AsyncMock()
        self.runtime.schedule_state_push = lambda immediate=False: None
        self.runtime.entities = lambda: []
        base = 128 | 256 | 1024 | 4 | 8 | 2048
        net = base | 16384 | 1 | 4096 | 32 | 16 | 512
        sources = ["Blu-ray", "NET", "TV Audio"]
        def receiver(features, source_list=sources):
            self.runtime.groups = lambda: [DeviceGroup("denon", "Home Theater", entities=[
                Entity("media_player.home_theater", "Home Theater", True, features,
                       {"source_list": source_list}, platform="denonavr")])]
        with patch("custom_components.melodious.persistent_notification"), \
                patch("custom_components.melodious.async_call_later") as later:
            receiver(net)
            await self.runtime.async_push()
            self.assertEqual(client.commit.await_count, 1)
            for features in (base, net, base, net, base):
                receiver(features)
                self.runtime._schedule_push_if_changed()
                await self.runtime.async_push()
            later.assert_not_called()
            self.assertEqual(client.commit.await_count, 1)
            # A source the receiver didn't have before is a real change.
            receiver(base, sources + ["Game"])
            self.runtime._schedule_push_if_changed()
            self.assertEqual(later.call_count, 1)
            self.runtime._timer = None
            await self.runtime.async_push()
            self.assertEqual(client.commit.await_count, 2)
            names = [c["name"] for c in client.commit.await_args.args[0]["devices"][0]["commands"]]
            self.assertEqual(names[-1], "Input Game")

    def test_build_includes_singles_beside_merged_devices(self):
        # Issue #162: _build used to return only the merged set whenever a
        # whole device was configured, dropping every single entity.
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        features = 128 | 256  # TURN_ON | TURN_OFF
        self.runtime.groups = lambda: [DeviceGroup(
            "dev_tv", "TV",
            entities=[Entity("media_player.tv", "TV", True, features, {})])]
        self.runtime.entities = lambda: [
            Entity("media_player.tv", "TV", True, features, {}),  # picked both ways
            Entity("input_button.movie", "Movie", state="unknown"),
            Entity("script.goodnight", "Good Night", state="off"),
        ]
        device_set = self.runtime._build()
        kinds = {d.get("key") or d.get("entity") for d in device_set["devices"]}
        self.assertEqual(
            kinds, {"media_player.tv", "input_button.movie", "script.goodnight"})
        listed = [
            i["entity"] for d in device_set["devices"] for i in d.get("entities") or []
        ] + [d["entity"] for d in device_set["devices"] if d.get("entity")]
        self.assertEqual(
            sorted(listed),
            ["input_button.movie", "media_player.tv", "script.goodnight"])
        singles = {d["entity"]: d for d in device_set["devices"] if d.get("entity")}
        self.assertEqual(
            singles["input_button.movie"]["commands"],
            [{"name": "Press", "value": "press"}])
        self.assertEqual(
            singles["script.goodnight"]["commands"],
            [{"name": "Run", "value": "turn_on"}])

    async def test_push_succeeds_with_hue_merged_device_and_single_script_after_recreate(self):
        # Issue #162 follow-up: after deleting/recreating the hub with an
        # existing merged Hue device (inch quotes in model, >32 entities,
        # unsupported sensors, v2 hub seed), pushing a single script alongside
        # the Hue device must emit a valid hub payload and not double-prefix.
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        client = self.runtime.client
        client.device_names = AsyncMock(return_value=set())
        client.ha_devices = AsyncMock(return_value=[{
            "key": "light.hue_strip",
            "name": "TV Backlight",
            "entities": [{"alias": "light.hue_strip", "entity": "light.hue_strip"}],
            "commands": [
                {"name": "PowerOn", "value": "light.hue_strip:turn_on"},
                {"name": "BrightnessUp", "value": "light.hue_strip:brightness_up"},
            ],
        }])
        client.features = AsyncMock(return_value={"ha-devices-v2"})
        client.commit = AsyncMock(return_value={
            "changed": True, "added": ["script.goodnight"], "updated": [], "removed": []})
        self.runtime.store.async_save = AsyncMock()
        self.runtime.schedule_state_push = lambda immediate=False: None
        self.runtime.sent = None  # freshly recreated hub entry
        hue_members = [
            Entity("sensor." + "hue_connectivity_" + "x" * 60, "Connectivity", True),
            Entity("light.hue_strip", "TV Backlight", True, 0,
                   {"supported_color_modes": ["onoff"]}),
            *[Entity(f"scene.hue_scene_{i:02d}", f"Scene {i}", True) for i in range(36)],
        ]
        self.runtime.groups = lambda: [DeviceGroup(
            "dev_hue", "TV Backlight",
            manufacturer="Signify",
            model='Hue Play gradient lightstrip 65"',
            entities=hue_members,
        )]
        self.runtime.entities = lambda: [Entity("script.goodnight", "Good Night", state="off")]
        with patch("custom_components.melodious.persistent_notification"):
            await self.runtime.async_push(force=True)
        payload = client.commit.await_args.args[0]
        by_key = {d.get("key") or d.get("entity"): d for d in payload["devices"]}
        self.assertIn("script.goodnight", by_key)
        hue = by_key["light.hue_strip"]
        self.assertNotIn('"', hue["model"])
        self.assertLessEqual(len(hue["entities"]), 32)
        self.assertTrue(all(len(i["alias"]) <= 64 for i in hue["entities"]))
        values = {c["name"]: c["value"] for c in hue["commands"]}
        self.assertEqual(values["BrightnessUp"], "light.hue_strip:brightness_up")

    async def test_push_notifies_on_retained_devices_and_keeps_hub_seed(self):
        # Issue #162: when an unpushed device ("Salon") is still used by MapList,
        # the hub retains it and commits other devices (like a single script).
        # HA must surface a clear notification naming Salon and MapList, keep
        # Salon's seed in case it is re-selected, and dismiss once cleared.
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        client = self.runtime.client
        client.device_names = AsyncMock(return_value=set())
        client.ha_devices = AsyncMock(return_value=[{
            "key": "scene.salon_legacy",
            "name": "Salon",
            "entities": [{"alias": "light.salon", "entity": "light.salon"}],
            "commands": [
                {"name": "PowerOn", "value": "light.salon:turn_on"},
                {"name": "PowerOff", "value": "light.salon:turn_off"},
            ],
        }])
        client.features = AsyncMock(return_value={"ha-devices-v2"})
        client.commit = AsyncMock(return_value={
            "ok": True,
            "changed": True,
            "added": ["Good Night"],
            "updated": [],
            "removed": [],
            "retained": [{"name": "Salon", "deviceId": "39000001", "usedBy": ["MapList"]}],
        })
        self.runtime.store.async_save = AsyncMock()
        self.runtime.schedule_state_push = lambda immediate=False: None
        self.runtime.sent = None  # freshly recreated hub entry
        self.runtime.groups = lambda: []
        self.runtime.entities = lambda: [Entity("script.goodnight", "Good Night", state="off")]
        with patch("custom_components.melodious.persistent_notification") as notif:
            await self.runtime.async_push(force=True)
            self.assertIn("script.goodnight", self.runtime.allowed)
            # Refusal notification dismissed; retained notification created naming Salon + MapList.
            notif.async_dismiss.assert_any_call(self.hass, "melodious_test")
            notif.async_create.assert_called_once()
            msg = notif.async_create.call_args.args[1]
            self.assertIn("Salon", msg)
            self.assertIn("MapList", msg)
            self.assertEqual(notif.async_create.call_args.kwargs["notification_id"], "melodious_test_retained")

            # Later, the user re-selects Salon alongside the script: Salon must
            # keep its hub key from _hub_seed, and once retained is empty the
            # retained notification is dismissed.
            notif.reset_mock()
            client.commit = AsyncMock(return_value={
                "ok": True, "changed": True, "added": [], "updated": ["Salon"],
                "removed": [], "retained": [],
            })
            self.runtime.groups = lambda: [DeviceGroup(
                "dev_salon", "Salon",
                entities=[Entity("light.salon", "Salon", True, 0, {"supported_color_modes": ["xy"]})],
            )]
            await self.runtime.async_push(force=True)
            payload = client.commit.await_args.args[0]
            salon_spec = next(d for d in payload["devices"] if d.get("name") == "Salon")
            self.assertEqual(salon_spec["key"], "scene.salon_legacy")
            notif.async_dismiss.assert_any_call(self.hass, "melodious_test_retained")

    async def test_push_refusal_keeps_hub_error_in_notification(self):
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        client = self.runtime.client
        client.device_names = AsyncMock(return_value=set())
        client.features = AsyncMock(return_value={"ha-devices-v2"})
        client.commit = AsyncMock(side_effect=HubRefused(
            "Salon: an activity, button or sequence still uses command poweron"))
        self.runtime.sent = {"webhook": "http://ha/api/webhook/hook", "devices": []}
        self.runtime.groups = lambda: []
        self.runtime.entities = lambda: [Entity("script.goodnight", "Good Night", state="off")]
        with patch("custom_components.melodious.persistent_notification") as notif:
            await self.runtime.async_push(force=True)
            notif.async_create.assert_called_once()
            self.assertIn(
                "Salon: an activity, button or sequence still uses command poweron",
                notif.async_create.call_args.args[1],
            )
            self.assertEqual(notif.async_create.call_args.kwargs["notification_id"], "melodious_test")

    async def test_legacy_push_flattens_groups_and_singles_once(self):
        # A hub without ha-devices-v2 gets one v1 device per entity, from the
        # merged devices and the single entities together, each exactly once.
        self.entry.data = {"ha_url": "http://ha", "webhook_id": "hook"}
        client = self.runtime.client
        client.device_names = AsyncMock(return_value=set())
        client.features = AsyncMock(return_value=set())
        client.commit = AsyncMock(return_value={
            "changed": True, "added": [], "updated": [], "removed": []})
        self.runtime.store.async_save = AsyncMock()
        self.runtime.schedule_state_push = lambda immediate=False: None
        self.runtime.sent = {"webhook": "http://ha/api/webhook/hook", "devices": []}
        features = 128 | 256  # TURN_ON | TURN_OFF
        self.runtime.groups = lambda: [DeviceGroup(
            "dev_tv", "TV",
            entities=[Entity("media_player.tv", "TV", True, features, {})])]
        self.runtime.entities = lambda: [
            Entity("media_player.tv", "TV", True, features, {}),  # picked both ways
            Entity("input_button.movie", "Movie", state="unknown"),
            Entity("script.goodnight", "Good Night", state="off"),
        ]
        with patch("custom_components.melodious.persistent_notification"):
            await self.runtime.async_push()
        device_set = client.commit.await_args.args[0]
        self.assertEqual(
            sorted(d["entity"] for d in device_set["devices"]),
            ["input_button.movie", "media_player.tv", "script.goodnight"])
        self.assertIn("press", self.runtime.allowed["input_button.movie"])
        self.assertIn("turn_on", self.runtime.allowed["script.goodnight"])


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
