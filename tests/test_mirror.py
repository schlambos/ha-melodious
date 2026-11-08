"""Tests for mirroring hub-only devices into Home Assistant (Phase 1).

The fixture is a sanitised extract of the owner's 2026-11-07 hub config:
no hub address, only the devices and activities this feature reads.
"""

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "melodious_mirror_test"
_package = types.ModuleType(PACKAGE)
_package.__path__ = [str(ROOT / "custom_components/melodious")]
sys.modules[PACKAGE] = _package


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.{name}", ROOT / f"custom_components/melodious/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE}.{name}"] = module
    spec.loader.exec_module(module)
    return module


catalogue = _load("catalogue")
const = _load("const")
mirror = _load("mirror")
CONFIG = json.loads((ROOT / "tests/fixtures/hub_activity_config.json").read_text())
NAKAMICHI = "81897247"
HIFI = "91684058"
TOPING = "91684059"
HA_LIGHT = "91684061"


class MirrorParsingTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def test_only_hub_only_devices_are_mirrorable(self):
        names = [device.name for device in self.cat.mirror_devices()]
        self.assertEqual(
            names,
            ["Google TV", "LG TV", "Nakamichi Amp", "HiFi", "Toping E30"],
        )
        self.assertNotIn("Living Room 1", names)
        self.assertNotIn("Living Room TV", names)

    def test_ha_backed_devices_are_detected_by_transport_or_marker(self):
        light = self.cat.device_by_id(HA_LIGHT)
        self.assertTrue(light.is_ha)
        self.assertEqual(light.transport, 64)
        self.assertEqual(light.ha_tag, None)
        self.assertEqual(light.manufacturer, "Home Assistant")
        self.assertEqual(light.model, "Home Assistant entity")

    def test_real_device_metadata_and_commands(self):
        amp = self.cat.device_by_id(NAKAMICHI)
        self.assertEqual(amp.name, "Nakamichi Amp")
        self.assertEqual(amp.manufacturer, "Nakamichi")
        self.assertEqual(amp.model, "Shockwafe Pro 7.1")
        self.assertEqual(amp.transport, 1)
        self.assertFalse(amp.is_ha)
        self.assertEqual(len(amp.commands), 55)
        self.assertIn("VolumeUp", amp.commands)
        self.assertIn("InputHdmiArc", amp.commands)

    def test_picker_labels_show_the_hardware(self):
        options = mirror.mirror_picker_options(self.cat)
        by_value = {option["value"]: option["label"] for option in options}
        self.assertEqual(by_value[NAKAMICHI], "Nakamichi Amp (Nakamichi Shockwafe Pro 7.1)")
        self.assertNotIn(HA_LIGHT, by_value)
        self.assertEqual([option["value"] for option in options],
                         sorted(by_value, key=lambda value: by_value[value].casefold()))


class MirrorButtonTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def specs(self, options):
        return mirror.mirror_button_specs(self.cat, options)

    def test_every_command_becomes_a_button(self):
        specs = self.specs([{"device_id": NAKAMICHI, "disabled_commands": []}])
        self.assertEqual(len(specs), 55)
        self.assertEqual({spec.device.id for spec in specs}, {NAKAMICHI})
        self.assertEqual(len({spec.command for spec in specs}), 55)
        self.assertIn("VolumeUp", {spec.command for spec in specs})

    def test_disabled_commands_are_left_out_case_insensitively(self):
        specs = self.specs([{
            "device_id": NAKAMICHI,
            "disabled_commands": ["VolumeUp", "volumeDOWN", "NotACommand"],
        }])
        commands = {spec.command for spec in specs}
        self.assertEqual(len(specs), 53)
        self.assertNotIn("VolumeUp", commands)
        self.assertNotIn("VolumeDown", commands)

    def test_the_three_gate_devices(self):
        specs = self.specs([
            {"device_id": NAKAMICHI, "disabled_commands": []},
            {"device_id": HIFI, "disabled_commands": []},
            {"device_id": TOPING, "disabled_commands": []},
        ])
        counts = {}
        for spec in specs:
            counts[spec.device.name] = counts.get(spec.device.name, 0) + 1
        self.assertEqual(counts, {"Nakamichi Amp": 55, "HiFi": 55, "Toping E30": 12})

    def test_a_home_assistant_device_is_never_mirrored_back(self):
        specs = self.specs([{"device_id": HA_LIGHT, "disabled_commands": []}])
        self.assertEqual(specs, [])

    def test_a_device_removed_from_the_hub_is_ignored(self):
        specs = self.specs([{"device_id": "99999999", "disabled_commands": []}])
        self.assertEqual(specs, [])

    def test_a_device_with_a_pushed_key_is_not_mirrored(self):
        pushed = catalogue.HubDevice(
            "42", "A pushed lamp", ("PowerOn",), False, key="light.lamp"
        )
        cat = catalogue.Catalogue((pushed,), ())
        options = {const.CONF_DEVICES: [{"key": "light.lamp"}]}
        self.assertEqual(mirror.mirror_picker_options(cat, options), [])
        self.assertEqual(mirror.mirror_button_specs(
            cat, [{"device_id": "42", "disabled_commands": []}],
            mirror.pushed_device_keys(options),
        ), [])
        # Without the pushed key it is a normal hub device.
        self.assertEqual(len(mirror.mirror_picker_options(cat)), 1)

    def test_stored_config_lookup(self):
        options = [{"device_id": NAKAMICHI, "disabled_commands": []}]
        self.assertEqual(mirror.mirror_device_config(options, NAKAMICHI)["device_id"], NAKAMICHI)
        self.assertIsNone(mirror.mirror_device_config(options, HIFI))
        self.assertIsNone(mirror.mirror_device_config(None, NAKAMICHI))


class PowerFeatureTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def test_discrete_power_uses_separate_on_and_off_commands(self):
        for device_id, on, off in (
            ("74521691", "PowerOn", "PowerOff"),
            (HIFI, "PowerOn", "PowerOff"),
            # The Google TV's power buttons are Home and Sleep.
            ("66690268", "Home", "Sleep"),
        ):
            device = self.cat.device_by_id(device_id)
            self.assertEqual(device.power_type, 1)
            self.assertEqual(device.power_on_command, on)
            self.assertEqual(device.power_off_command, off)

    def test_toggle_power_uses_the_same_command_for_both_directions(self):
        amp = self.cat.device_by_id(NAKAMICHI)
        self.assertEqual(amp.power_type, 2)
        self.assertEqual(amp.power_on_command, "PowerToggle")
        self.assertEqual(amp.power_off_command, "PowerToggle")

    def test_a_device_without_a_power_feature_has_no_switch_commands(self):
        toping = self.cat.device_by_id(TOPING)
        self.assertIsNone(toping.power_type)
        self.assertIsNone(toping.power_on_command)
        self.assertIsNone(toping.power_off_command)

    def test_an_unknown_power_type_is_not_a_power_feature(self):
        config = {"deviceList": {"DevicesWithFeatures": [{
            "Device": {"Id-": 1, "Name": "Odd"},
            "DeviceFeatures": [{"__type": "PowerFeature", "PowerTypeId": 9}],
            "Commands": [],
        }]}}
        device = catalogue.Catalogue.from_config(config).device_by_id("1")
        self.assertIsNone(device.power_type)
        self.assertIsNone(device.power_on_command)


class InputFeatureTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def test_inputs_keep_the_hubs_order_and_names(self):
        amp = self.cat.device_by_id(NAKAMICHI)
        self.assertEqual(amp.inputs, (
            "Aux", "Bluetooth", "Coaxial", "HDMI Arc", "HDMI 2",
            "HDMI 3", "HDMI 4", "Optical", "USB",
        ))
        self.assertEqual(self.cat.device_by_id("66690268").inputs, ("Tuner",))

    def test_a_device_without_an_input_feature_has_none(self):
        self.assertIsNone(self.cat.device_by_id(TOPING).inputs)

    def test_null_input_entries_are_dropped(self):
        config = {"deviceList": {"DevicesWithFeatures": [{
            "Device": {"Id-": 1, "Name": "Inputs"},
            "DeviceFeatures": [{"__type": "InputFeature", "Inputs": [
                {"InputName": "Aux"}, {"InputName": None}, {}, {"InputName": ""},
            ]}],
            "Commands": [],
        }]}}
        device = catalogue.Catalogue.from_config(config).device_by_id("1")
        self.assertEqual(device.inputs, ("Aux",))

    def test_an_empty_input_feature_is_none(self):
        config = {"deviceList": {"DevicesWithFeatures": [{
            "Device": {"Id-": 1, "Name": "Empty"},
            "DeviceFeatures": [{"__type": "InputFeature", "Inputs": []}],
            "Commands": [],
        }]}}
        device = catalogue.Catalogue.from_config(config).device_by_id("1")
        self.assertIsNone(device.inputs)


class ActivityRoleTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def test_member_devices_and_role_types(self):
        watch_tv = self.cat.activity("Watch TV")
        self.assertEqual(watch_tv.device_ids, ("66690268", NAKAMICHI, "74521691"))
        self.assertEqual(watch_tv.roles, (
            ("PlayMovieActivityRole", "66690268"),
            ("VolumeActivityRole", NAKAMICHI),
            ("DisplayActivityRole", "74521691"),
        ))
        self.assertEqual(watch_tv.role_for(NAKAMICHI), "VolumeActivityRole")
        self.assertIsNone(watch_tv.role_for(HIFI))

    def test_poweroff_is_not_an_activity_and_has_no_roles(self):
        self.assertIsNone(self.cat.activity("-1"))
        self.assertEqual([a.name for a in self.cat.activities],
                         ["Watch TV", "Music", "Nintendo Switch"])


class PowerDerivationTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)
        self.watch_tv = "1878029666"

    def test_a_member_of_the_running_activity_is_on(self):
        self.assertTrue(mirror.device_power_on(self.cat, self.watch_tv, NAKAMICHI))
        self.assertTrue(mirror.device_power_on(self.cat, self.watch_tv, "74521691"))
        self.assertTrue(mirror.device_power_on(self.cat, self.watch_tv, "66690268"))

    def test_a_device_outside_the_running_activity_is_off(self):
        self.assertFalse(mirror.device_power_on(self.cat, self.watch_tv, HIFI))
        self.assertFalse(mirror.device_power_on(self.cat, self.watch_tv, TOPING))

    def test_poweroff_and_no_activity_mean_off(self):
        for activity_id in ("-1", None, "", "999"):
            self.assertFalse(
                mirror.device_power_on(self.cat, activity_id, NAKAMICHI)
            )

    def test_activity_name_and_role_are_idle_outside_the_activity(self):
        self.assertEqual(
            mirror.device_activity_name(self.cat, self.watch_tv, NAKAMICHI),
            "Watch TV",
        )
        self.assertEqual(
            mirror.device_activity_role(self.cat, self.watch_tv, NAKAMICHI),
            "VolumeActivityRole",
        )
        self.assertEqual(
            mirror.device_activity_name(self.cat, self.watch_tv, HIFI), "Idle"
        )
        self.assertIsNone(
            mirror.device_activity_role(self.cat, self.watch_tv, HIFI)
        )
        self.assertEqual(
            mirror.device_activity_name(self.cat, "-1", NAKAMICHI), "Idle"
        )

    def test_activity_members_map_names_to_roles(self):
        self.assertEqual(
            mirror.activity_members(self.cat, self.cat.activity("Watch TV")),
            {
                "Google TV": "PlayMovieActivityRole",
                "Nakamichi Amp": "VolumeActivityRole",
                "LG TV": "DisplayActivityRole",
            },
        )
        self.assertEqual(mirror.activity_members(self.cat, None), {})

    def test_only_devices_with_a_power_feature_get_a_switch(self):
        options = [{"device_id": device_id, "disabled_commands": []} for device_id in (
            "66690268", "74521691", NAKAMICHI, HIFI, TOPING,
        )]
        devices = mirror.mirror_power_devices(self.cat, options)
        self.assertEqual(
            [device.name for device in devices],
            ["Google TV", "LG TV", "Nakamichi Amp", "HiFi"],
        )

    def test_only_devices_with_inputs_get_a_select(self):
        options = [{"device_id": device_id, "disabled_commands": []} for device_id in (
            "66690268", "74521691", NAKAMICHI, HIFI, TOPING,
        )]
        devices = mirror.mirror_input_devices(self.cat, options)
        self.assertEqual(
            [device.name for device in devices],
            ["Google TV", "LG TV", "Nakamichi Amp", "HiFi"],
        )

    def test_ha_and_missing_devices_are_not_mirrored(self):
        options = [
            {"device_id": HA_LIGHT, "disabled_commands": []},
            {"device_id": "99999999", "disabled_commands": []},
        ]
        self.assertEqual(mirror.mirror_power_devices(self.cat, options), [])
        self.assertEqual(mirror.mirror_input_devices(self.cat, options), [])
        self.assertEqual(mirror.mirrored_devices(self.cat, options), [])


class InputCommandTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def test_input_names_map_to_the_hubs_command_spelling(self):
        amp = self.cat.device_by_id(NAKAMICHI)
        self.assertEqual(mirror.input_command(amp, "HDMI Arc"), "InputHdmiArc")
        self.assertEqual(mirror.input_command(amp, "HDMI 2"), "InputHdmi2")
        self.assertEqual(mirror.input_command(amp, "Aux"), "InputAux")
        self.assertEqual(mirror.input_command(amp, "USB"), "InputUsb")

    def test_an_input_without_a_command_is_unknown(self):
        self.assertIsNone(
            mirror.input_command(self.cat.device_by_id("66690268"), "Tuner")
        )
        self.assertIsNone(
            mirror.input_command(self.cat.device_by_id(NAKAMICHI), "Not An Input")
        )

    def test_activity_options_end_with_poweroff(self):
        self.assertEqual(
            mirror.activity_options(self.cat),
            ["Watch TV", "Music", "Nintendo Switch", "PowerOff"],
        )


if __name__ == "__main__":
    unittest.main()
