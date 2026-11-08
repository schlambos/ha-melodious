"""Tests for custom_components/melodious/catalogue.py (the hub remote's name rules).

    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("catalogue", ROOT / "custom_components/melodious/catalogue.py")
catalogue = importlib.util.module_from_spec(_spec)
sys.modules["catalogue"] = catalogue
_spec.loader.exec_module(catalogue)

CONFIG = {
    "deviceList": {"DevicesWithFeatures": [
        {"Device": {"Id-": 66690268, "Name": "Google TV"},
         "Commands": [{"Name": "DirectionUp", "TransportType": 1}, {"Name": "Home", "TransportType": 1}]},
        {"Device": {"Id-": 91684109, "Name": "LG webOS TV"},
         "Commands": [{"Name": "PowerOn", "TransportType": 64}, {"Name": "Input HDMI 1", "TransportType": 64}]},
        {"Device": {"Id-": 1, "Name": ""}, "Commands": []},
    ]},
    "activityList": {"Activities": [
        {"Id-": 1878029666, "Name": "Watch TV"},
        {"Id-": 1878030269, "Name": "TV"},
        {"Id-": -1, "Name": "PowerOff"},
    ]},
}


def state(result):
    reply = {"cmd": "harmony.engine?getCurrentActivity", "code": 200, "data": {"result": result}}
    return {"ok": True, "reply": json.dumps(reply) + "\n"}


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.cat = catalogue.Catalogue.from_config(CONFIG)

    def test_devices_sit_beside_their_commands(self):
        self.assertEqual([d.name for d in self.cat.devices], ["Google TV", "LG webOS TV"])
        self.assertEqual(self.cat.devices[0].commands, ("DirectionUp", "Home"))
        self.assertFalse(self.cat.devices[0].is_ha)
        self.assertTrue(self.cat.devices[1].is_ha)

    def test_poweroff_is_not_an_activity(self):
        self.assertEqual([a.name for a in self.cat.activities], ["Watch TV", "TV"])

    def test_resolution_order(self):
        self.assertEqual(self.cat.device("Google TV").id, "66690268")
        self.assertEqual(self.cat.device(" 91684109 ").name, "LG webOS TV")
        self.assertEqual(self.cat.device("google tv").name, "Google TV")
        self.assertIsNone(self.cat.device("Roku"))
        self.assertEqual(self.cat.activity("TV").id, "1878030269")
        self.assertEqual(self.cat.activity("1878029666").name, "Watch TV")

    def test_commands_keep_the_hubs_spelling(self):
        device = self.cat.device("LG webOS TV")
        self.assertEqual(catalogue.resolve_command(device, "input hdmi 1"), "Input HDMI 1")
        self.assertIsNone(catalogue.resolve_command(device, "DPAD_UP"))

    def test_current_activity(self):
        self.assertEqual(self.cat.activity_name(catalogue.current_activity_id(state("1878030269"))), "TV")
        self.assertEqual(self.cat.activity_name(catalogue.current_activity_id(state("-1"))), "PowerOff")
        self.assertEqual(self.cat.activity_name(catalogue.current_activity_id({"reply": "garbage"})), "PowerOff")

    def test_engine_errors(self):
        refused = json.dumps({"code": 565, "msg": "Device not found for device id:999"})
        self.assertEqual(catalogue.engine_error(refused), "Device not found for device id:999")
        self.assertIsNone(catalogue.engine_error(json.dumps({"code": 200, "msg": "OK"})))
        self.assertIsNone(catalogue.engine_error(""))

    def test_state_version_is_found_through_the_engine_wrapping(self):
        digest = {"cmd": "connect.statedigest?get", "code": 200,
                  "data": {"result": {"stateVersion": 42, "activityId": 7}}}
        reply = json.dumps(digest)
        self.assertEqual(catalogue.extract_state_version(reply), "42")
        # The hub hands the digest over as a JSON string inside the reply.
        self.assertEqual(
            catalogue.extract_state_version(json.dumps({"reply": reply})), "42"
        )
        self.assertEqual(
            catalogue.extract_state_version({"reply": digest}), "42"
        )
        self.assertIsNone(catalogue.extract_state_version(None))
        self.assertIsNone(catalogue.extract_state_version("{}"))
        self.assertIsNone(catalogue.extract_state_version("garbage"))


if __name__ == "__main__":
    unittest.main()
