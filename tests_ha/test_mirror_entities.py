"""HA 2026.1.2 runtime tests for Phase 2's power, input and activity entities."""

import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import HomeAssistantError

from custom_components.melodious import button, select, sensor, switch
from custom_components.melodious.button import HubActivityButton
from custom_components.melodious.catalogue import Catalogue
from custom_components.melodious.const import CONF_MIRROR_DEVICES
from custom_components.melodious.coordinator import MelodiousCoordinator
from custom_components.melodious.select import HubActivitySelect, MirrorInputSelect
from custom_components.melodious.sensor import HubActivitySensor, MirrorActivitySensor
from custom_components.melodious.switch import MirrorPowerSwitch

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "tests/fixtures/hub_activity_config.json").read_text())
GOOGLE = "66690268"
LG = "74521691"
NAKAMICHI = "81897247"
HIFI = "91684058"
TOPING = "91684059"
WATCH_TV = "1878029666"
MUSIC = "1878029883"
HOST = "hub.example"
ALL_DEVICES = [GOOGLE, LG, NAKAMICHI, HIFI, TOPING]


def entry(**overrides):
    values = {
        "entry_id": "entry1",
        "unique_id": HOST,
        "title": "Melodious hub",
        "data": {"host": HOST, "port": 8080},
        "options": {},
        "async_on_unload": lambda callback: None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def coordinator(catalogue, activity_id=None, client=None):
    return SimpleNamespace(
        data=(catalogue, activity_id),
        catalogue=catalogue,
        activity_id=activity_id,
        client=client or SimpleNamespace(),
        last_update_success=True,
        async_request_refresh=AsyncMock(),
    )


def mirror_options(*device_ids):
    return {
        CONF_MIRROR_DEVICES: [
            {"device_id": device_id, "disabled_commands": []}
            for device_id in device_ids
        ]
    }


class MirrorSwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cat = Catalogue.from_config(CONFIG)

    async def test_setup_creates_a_switch_only_for_devices_with_power(self):
        data = entry(options=mirror_options(*ALL_DEVICES))
        data.runtime_data = SimpleNamespace(coordinator=coordinator(self.cat))
        added = []
        await switch.async_setup_entry(None, data, added.extend)
        self.assertEqual(
            sorted(entity.device_info["name"] for entity in added),
            ["Google TV", "HiFi", "LG TV", "Nakamichi Amp"],
        )
        self.assertNotIn("Toping E30", [e.device_info["name"] for e in added])

    async def test_state_follows_activity_membership(self):
        data = entry()
        watch = MirrorPowerSwitch(
            coordinator(self.cat, WATCH_TV), data, self.cat.device_by_id(NAKAMICHI)
        )
        hifi = MirrorPowerSwitch(
            coordinator(self.cat, WATCH_TV), data, self.cat.device_by_id(HIFI)
        )
        idle = MirrorPowerSwitch(
            coordinator(self.cat, "-1"), data, self.cat.device_by_id(NAKAMICHI)
        )
        self.assertTrue(watch.is_on)
        self.assertFalse(hifi.is_on)
        self.assertFalse(idle.is_on)
        self.assertTrue(watch.assumed_state)

    async def test_discrete_switch_sends_power_on_and_power_off(self):
        client = SimpleNamespace(
            send_command=AsyncMock(return_value={"reply": '{"code":200}'})
        )
        data = entry()
        hifi = MirrorPowerSwitch(
            coordinator(self.cat, None, client), data, self.cat.device_by_id(HIFI)
        )
        await hifi.async_turn_on()
        client.send_command.assert_awaited_once_with(HIFI, "PowerOn")
        client.send_command.reset_mock()
        await hifi.async_turn_off()
        client.send_command.assert_awaited_once_with(HIFI, "PowerOff")

    async def test_toggle_switch_sends_powertoggle_both_ways(self):
        client = SimpleNamespace(
            send_command=AsyncMock(return_value={"reply": '{"code":200}'})
        )
        data = entry()
        amp = MirrorPowerSwitch(
            coordinator(self.cat, None, client), data, self.cat.device_by_id(NAKAMICHI)
        )
        await amp.async_turn_on()
        client.send_command.assert_awaited_once_with(NAKAMICHI, "PowerToggle")
        client.send_command.reset_mock()
        await amp.async_turn_off()
        client.send_command.assert_awaited_once_with(NAKAMICHI, "PowerToggle")

    async def test_google_tv_uses_home_and_sleep(self):
        client = SimpleNamespace(
            send_command=AsyncMock(return_value={"reply": '{"code":200}'})
        )
        data = entry()
        google = MirrorPowerSwitch(
            coordinator(self.cat, None, client), data, self.cat.device_by_id(GOOGLE)
        )
        await google.async_turn_on()
        client.send_command.assert_awaited_once_with(GOOGLE, "Home")
        client.send_command.reset_mock()
        await google.async_turn_off()
        client.send_command.assert_awaited_once_with(GOOGLE, "Sleep")

    async def test_an_engine_refusal_is_an_error(self):
        client = SimpleNamespace(
            send_command=AsyncMock(
                return_value={"reply": json.dumps({"code": 565, "msg": "no"})}
            )
        )
        data = entry()
        hifi = MirrorPowerSwitch(
            coordinator(self.cat, None, client), data, self.cat.device_by_id(HIFI)
        )
        with self.assertRaises(HomeAssistantError):
            await hifi.async_turn_on()


class MirrorInputSelectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cat = Catalogue.from_config(CONFIG)

    async def test_setup_creates_input_selects_and_the_hub_activity_select(self):
        data = entry(options=mirror_options(*ALL_DEVICES))
        data.runtime_data = SimpleNamespace(coordinator=coordinator(self.cat))
        added = []
        await select.async_setup_entry(None, data, added.extend)
        inputs = [entity for entity in added if isinstance(entity, MirrorInputSelect)]
        hubs = [entity for entity in added if isinstance(entity, HubActivitySelect)]
        self.assertEqual(
            sorted(entity.device_info["name"] for entity in inputs),
            ["Google TV", "HiFi", "LG TV", "Nakamichi Amp"],
        )
        self.assertEqual(len(hubs), 1)

    async def test_options_are_the_devices_inputs(self):
        entity = MirrorInputSelect(
            coordinator(self.cat), entry(), self.cat.device_by_id(NAKAMICHI)
        )
        self.assertEqual(
            entity.options,
            ["Aux", "Bluetooth", "Coaxial", "HDMI Arc", "HDMI 2",
             "HDMI 3", "HDMI 4", "Optical", "USB"],
        )
        self.assertIsNone(entity.current_option)

    async def test_selecting_an_input_sends_the_matching_command(self):
        client = SimpleNamespace(
            send_command=AsyncMock(return_value={"reply": '{"code":200}'})
        )
        entity = MirrorInputSelect(
            coordinator(self.cat, None, client), entry(),
            self.cat.device_by_id(NAKAMICHI),
        )
        entity.async_write_ha_state = Mock()
        await entity.async_select_option("HDMI Arc")
        client.send_command.assert_awaited_once_with(NAKAMICHI, "InputHdmiArc")
        self.assertEqual(entity.current_option, "HDMI Arc")

    async def test_an_input_without_a_command_raises(self):
        client = SimpleNamespace(send_command=AsyncMock())
        entity = MirrorInputSelect(
            coordinator(self.cat, None, client), entry(),
            self.cat.device_by_id(GOOGLE),
        )
        entity.async_write_ha_state = Mock()
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("Tuner")
        client.send_command.assert_not_awaited()

    async def test_an_unknown_option_raises(self):
        client = SimpleNamespace(send_command=AsyncMock())
        entity = MirrorInputSelect(
            coordinator(self.cat, None, client), entry(),
            self.cat.device_by_id(NAKAMICHI),
        )
        with self.assertRaises(HomeAssistantError):
            await entity.async_select_option("Not An Input")
        client.send_command.assert_not_awaited()

    async def test_an_activity_change_resets_the_optimistic_option(self):
        client = SimpleNamespace(
            send_command=AsyncMock(return_value={"reply": '{"code":200}'})
        )
        data = coordinator(self.cat, WATCH_TV, client)
        entity = MirrorInputSelect(data, entry(), self.cat.device_by_id(NAKAMICHI))
        entity.async_write_ha_state = Mock()
        await entity.async_select_option("Aux")
        self.assertEqual(entity.current_option, "Aux")
        # An unchanged poll keeps the optimistic pick.
        entity._handle_coordinator_update()
        self.assertEqual(entity.current_option, "Aux")
        # A new activity means the hub no longer knows the input.
        data.activity_id = MUSIC
        entity._handle_coordinator_update()
        self.assertIsNone(entity.current_option)


class HubActivitySelectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cat = Catalogue.from_config(CONFIG)

    async def test_options_and_state_track_the_running_activity(self):
        entity = HubActivitySelect(coordinator(self.cat, WATCH_TV), entry())
        self.assertEqual(
            entity.options,
            ["Watch TV", "Music", "Nintendo Switch", "PowerOff"],
        )
        self.assertEqual(entity.current_option, "Watch TV")
        idle = HubActivitySelect(coordinator(self.cat, "-1"), entry())
        self.assertEqual(idle.current_option, "PowerOff")

    async def test_selecting_an_activity_starts_it(self):
        client = SimpleNamespace(
            run_activity=AsyncMock(return_value={"ok": True})
        )
        data = coordinator(self.cat, None, client)
        entity = HubActivitySelect(data, entry())
        await entity.async_select_option("Music")
        client.run_activity.assert_awaited_once_with(MUSIC)
        data.async_request_refresh.assert_awaited_once()

    async def test_poweroff_uses_minus_one(self):
        client = SimpleNamespace(
            run_activity=AsyncMock(return_value={"ok": True})
        )
        data = coordinator(self.cat, WATCH_TV, client)
        entity = HubActivitySelect(data, entry())
        await entity.async_select_option("PowerOff")
        client.run_activity.assert_awaited_once_with("-1")


class HubActivityButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cat = Catalogue.from_config(CONFIG)

    async def test_setup_adds_one_button_per_activity_plus_poweroff(self):
        data = entry(options=mirror_options())
        data.runtime_data = SimpleNamespace(coordinator=coordinator(self.cat))
        added = []
        await button.async_setup_entry(None, data, added.extend)
        self.assertEqual(
            sorted(entity.name for entity in added),
            ["Music", "Nintendo Switch", "Power Off", "Watch TV"],
        )

    async def test_pressing_an_activity_button_starts_it(self):
        client = SimpleNamespace(
            run_activity=AsyncMock(return_value={"ok": True})
        )
        data = coordinator(self.cat, None, client)
        entity = HubActivityButton(data, entry(), self.cat.activity("Watch TV"))
        await entity.async_press()
        client.run_activity.assert_awaited_once_with(WATCH_TV)
        data.async_request_refresh.assert_awaited_once()

    async def test_the_power_off_button_stops_everything(self):
        client = SimpleNamespace(
            run_activity=AsyncMock(return_value={"ok": True})
        )
        data = coordinator(self.cat, WATCH_TV, client)
        entity = HubActivityButton(data, entry(), None)
        self.assertEqual(entity.name, "Power Off")
        await entity.async_press()
        client.run_activity.assert_awaited_once_with("-1")


class ActivitySensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cat = Catalogue.from_config(CONFIG)

    async def test_setup_adds_one_sensor_per_mirrored_device_and_the_hub(self):
        data = entry(options=mirror_options(*ALL_DEVICES))
        data.runtime_data = SimpleNamespace(coordinator=coordinator(self.cat))
        added = []
        await sensor.async_setup_entry(None, data, added.extend)
        mirrors = [entity for entity in added if isinstance(entity, MirrorActivitySensor)]
        hubs = [entity for entity in added if isinstance(entity, HubActivitySensor)]
        self.assertEqual(len(mirrors), len(ALL_DEVICES))
        self.assertEqual(len(hubs), 1)

    async def test_a_mirrored_device_reports_its_running_activity_and_role(self):
        entity = MirrorActivitySensor(
            coordinator(self.cat, WATCH_TV), entry(),
            self.cat.device_by_id(NAKAMICHI),
        )
        self.assertEqual(entity.native_value, "Watch TV")
        self.assertEqual(
            entity.extra_state_attributes,
            {"activity_id": WATCH_TV, "role": "VolumeActivityRole"},
        )
        self.assertEqual(
            entity.entity_category, sensor.EntityCategory.DIAGNOSTIC
        )

    async def test_a_device_outside_the_running_activity_is_idle(self):
        entity = MirrorActivitySensor(
            coordinator(self.cat, WATCH_TV), entry(), self.cat.device_by_id(HIFI)
        )
        self.assertEqual(entity.native_value, "Idle")
        self.assertEqual(
            entity.extra_state_attributes, {"activity_id": None, "role": None}
        )

    async def test_the_hub_sensor_lists_members_and_roles(self):
        entity = HubActivitySensor(coordinator(self.cat, WATCH_TV), entry())
        self.assertEqual(entity.native_value, "Watch TV")
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "activity_id": WATCH_TV,
                "members": {
                    "Google TV": "PlayMovieActivityRole",
                    "Nakamichi Amp": "VolumeActivityRole",
                    "LG TV": "DisplayActivityRole",
                },
            },
        )

    async def test_the_hub_sensor_is_poweroff_when_idle(self):
        entity = HubActivitySensor(coordinator(self.cat, "-1"), entry())
        self.assertEqual(entity.native_value, "PowerOff")
        self.assertEqual(
            entity.extra_state_attributes,
            {"activity_id": "-1", "members": {}},
        )


class CoordinatorPollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cat = Catalogue.from_config(CONFIG)
        self.state = {
            "reply": json.dumps({
                "cmd": "harmony.engine?getCurrentActivity",
                "code": 200,
                "data": {"result": WATCH_TV},
            })
        }

    async def test_first_poll_is_full_then_unchanged_polls_are_cheap(self):
        client = SimpleNamespace(
            state_version=AsyncMock(side_effect=[(True, "7"), (False, "7")]),
            activity_config=AsyncMock(return_value=CONFIG),
            activity_state=AsyncMock(return_value=self.state),
        )
        data = MelodiousCoordinator(SimpleNamespace(), entry(), client)
        first = await data._async_update_data()
        data.data = first  # what DataUpdateCoordinator does after a refresh
        self.assertEqual(first[1], WATCH_TV)
        client.state_version.assert_awaited_once_with(None)
        client.activity_config.assert_awaited_once()
        client.activity_state.assert_awaited_once()

        second = await data._async_update_data()
        self.assertIs(second, first)
        client.state_version.assert_awaited_with("7")
        client.activity_config.assert_awaited_once()
        client.activity_state.assert_awaited_once()

    async def test_a_changed_version_fetches_the_full_state(self):
        client = SimpleNamespace(
            state_version=AsyncMock(side_effect=[(True, "7"), (True, "8")]),
            activity_config=AsyncMock(return_value=CONFIG),
            activity_state=AsyncMock(return_value=self.state),
        )
        data = MelodiousCoordinator(SimpleNamespace(), entry(), client)
        data.data = await data._async_update_data()
        data.data = await data._async_update_data()
        self.assertEqual(client.activity_config.await_count, 2)
        self.assertEqual(client.activity_state.await_count, 2)


if __name__ == "__main__":
    unittest.main()
