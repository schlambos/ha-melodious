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


if __name__ == "__main__":
    unittest.main()
