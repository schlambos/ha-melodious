"""Single entities (CONF_ENTITIES) beside merged whole devices (CONF_DEVICES).

Regression tests for issue #162: whenever a whole device was configured, the
runtime built only the merged set and single entities (input_button, script,
...) never reached the hub. Like test_devices.py, devices.py loads straight
from its file; no hub and no Home Assistant instance is involved.

    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "melodious_devices_combined", ROOT / "custom_components/melodious/devices.py")
devices = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = devices
SPEC.loader.exec_module(devices)
Entity = devices.Entity

WEBHOOK = "http://ha.example:8123/api/webhook/abc"
TV_FEATURES = devices.MP_TURN_ON | devices.MP_TURN_OFF | devices.MP_VOLUME_STEP


def tv_group(name="Living Room TV", device_id="dev_tv", entity_id="media_player.tv"):
    return devices.DeviceGroup(
        device_id, name,
        entities=[Entity(entity_id, name, True, TV_FEATURES, {})])


def button():
    return Entity("input_button.movie_time", "Movie Time", True)


def script():
    return Entity("script.good_night", "Good Night", True)


def combined(groups, entities, previous=None, taken_names=None):
    return devices.build_combined_set(
        WEBHOOK, groups, entities, {}, {}, previous or {},
        taken_names=taken_names)


def previous_by_key_and_entity(device_set):
    """Index a sent set the way MelodiousRuntime._previous does."""
    previous = {}
    for device in device_set["devices"]:
        if device.get("key"):
            previous[device["key"]] = device
            for item in device.get("entities", []):
                previous.setdefault(item["entity"], device)
        elif device.get("entity"):
            previous[device["entity"]] = device
    return previous


def assert_hub_accepts_mixed(test, device_set):
    """codexactivity haSetFrom's set-level rules, for a mixed v1/v2 list."""
    test.assertLessEqual(len(device_set["devices"]), devices.MAX_DEVICES)
    keys, names, entities = set(), set(), set()
    for d in device_set["devices"]:
        key = d.get("key") or d.get("entity")
        test.assertNotIn(key, keys, f"{key} listed twice")
        keys.add(key)
        test.assertNotIn(d["name"].lower(), names, f"two devices named {d['name']}")
        names.add(d["name"].lower())
        members = [i["entity"] for i in d.get("entities") or []] or [d["entity"]]
        for entity_id in members:
            test.assertNotIn(entity_id, entities, f"{entity_id} in two devices")
            entities.add(entity_id)
        test.assertTrue(0 < len(d["commands"]) <= devices.MAX_COMMANDS)
        for c in d["commands"]:
            test.assertTrue(0 < len(c["value"]) <= devices.MAX_VALUE, c)
            test.assertFalse(re.search(r'[\x00-\x1f\x7f"\\%{}]', c["value"]), c)


class CombinedSetTests(unittest.TestCase):
    def test_merged_and_single_devices_coexist(self):
        device_set, suppressed = combined([tv_group()], [button(), script()])
        self.assertEqual(suppressed, [])
        merged = [d for d in device_set["devices"] if d.get("key")]
        singles = {d["entity"]: d for d in device_set["devices"] if d.get("entity")}
        self.assertEqual([d["key"] for d in merged], ["media_player.tv"])
        self.assertEqual(
            merged[0]["entities"],
            [{"alias": "media_player.tv", "entity": "media_player.tv"}])
        self.assertEqual(sorted(singles), ["input_button.movie_time", "script.good_night"])
        self.assertEqual(
            singles["input_button.movie_time"]["commands"],
            [{"name": "Press", "value": "press"}])
        self.assertEqual(
            singles["script.good_night"]["commands"],
            [{"name": "Run", "value": "turn_on"}])
        assert_hub_accepts_mixed(self, device_set)

    def test_allowed_values_cover_both_shapes(self):
        device_set, _ = combined([tv_group()], [button(), script()])
        allowed = devices.allowed_values(device_set)
        self.assertEqual(allowed["input_button.movie_time"], {"press"})
        self.assertEqual(allowed["script.good_night"], {"turn_on"})
        self.assertIn("media_player.tv:turn_on", allowed["media_player.tv"])
        self.assertIn("media_player.tv:volume_up", allowed["media_player.tv"])

    def test_single_named_like_a_merged_device_is_renamed(self):
        namesake = Entity("switch.tv_power", "Living Room TV", True)
        device_set, _ = combined([tv_group()], [namesake])
        by_key = {d.get("key") or d.get("entity"): d for d in device_set["devices"]}
        self.assertEqual(by_key["media_player.tv"]["name"], "Living Room TV")
        self.assertEqual(by_key["switch.tv_power"]["name"], "Living Room TV (HA)")
        assert_hub_accepts_mixed(self, device_set)

    def test_single_name_collision_with_hub_and_merged_names_stays_unique(self):
        namesake = Entity("switch.tv_power", "Living Room TV", True)
        device_set, _ = combined(
            [tv_group()], [namesake], taken_names={"Living Room TV"})
        names = [d["name"] for d in device_set["devices"]]
        self.assertEqual(len({n.lower() for n in names}), len(names))
        assert_hub_accepts_mixed(self, device_set)

    def test_entity_picked_both_ways_appears_once(self):
        also_single = Entity("media_player.tv", "Living Room TV", True, TV_FEATURES, {})
        device_set, _ = combined([tv_group()], [also_single, button()])
        self.assertEqual(
            [d.get("entity") for d in device_set["devices"]],
            [None, "input_button.movie_time"])
        listed = [
            i["entity"] for d in device_set["devices"] for i in d.get("entities") or []
        ] + [d["entity"] for d in device_set["devices"] if d.get("entity")]
        self.assertEqual(sorted(listed), ["input_button.movie_time", "media_player.tv"])
        assert_hub_accepts_mixed(self, device_set)

    def test_suppressed_cast_twin_entity_is_still_offered_as_single(self):
        real = devices.DeviceGroup(
            "real", "Living Room TV",
            entities=[Entity("media_player.real_tv", "TV", True, TV_FEATURES, {},
                             platform="androidtv_remote")])
        twin = devices.DeviceGroup(
            "twin", "Living Room TV",
            entities=[Entity("media_player.cast_twin", "Cast", True, TV_FEATURES, {},
                             platform="cast")])
        twin_single = Entity("media_player.cast_twin", "Cast TV", True, TV_FEATURES, {})
        device_set, suppressed = combined([real, twin], [twin_single])
        self.assertEqual(suppressed, ["Living Room TV"])
        kinds = {d.get("key") or d.get("entity") for d in device_set["devices"]}
        self.assertEqual(kinds, {"media_player.real_tv", "media_player.cast_twin"})
        single = next(d for d in device_set["devices"] if d.get("entity"))
        self.assertEqual(single["entity"], "media_player.cast_twin")
        assert_hub_accepts_mixed(self, device_set)

    def test_total_stays_capped_at_max_devices(self):
        groups = [
            devices.DeviceGroup(
                f"dev{n}", f"Device {n:02d}",
                entities=[Entity(f"switch.d{n}", f"Device {n:02d}", True)])
            for n in range(40)
        ]
        singles = [Entity(f"input_button.s{n}", f"Single {n:02d}", True) for n in range(40)]
        device_set, _ = combined(groups, singles)
        self.assertEqual(len(device_set["devices"]), devices.MAX_DEVICES)
        self.assertEqual(sum(1 for d in device_set["devices"] if d.get("key")), 40)
        self.assertEqual(sum(1 for d in device_set["devices"] if d.get("entity")), 24)
        assert_hub_accepts_mixed(self, device_set)

    def test_merged_devices_keep_first_claim_on_the_cap(self):
        groups = [
            devices.DeviceGroup(
                f"dev{n}", f"Device {n:02d}",
                entities=[Entity(f"switch.d{n}", f"Device {n:02d}", True)])
            for n in range(70)
        ]
        device_set, _ = combined(groups, [button(), script()])
        self.assertEqual(len(device_set["devices"]), devices.MAX_DEVICES)
        self.assertTrue(all(d.get("key") for d in device_set["devices"]))

    def test_groups_only_matches_build_merged_set(self):
        groups = [tv_group()]
        merged, suppressed = devices.build_merged_set(WEBHOOK, groups, {}, {}, {})
        self.assertEqual(combined(groups, []), (merged, suppressed))

    def test_singles_only_matches_build_device_set(self):
        singles = [button(), script()]
        device_set, suppressed = combined([], singles)
        self.assertEqual(suppressed, [])
        self.assertEqual(device_set, devices.build_device_set(WEBHOOK, singles))

    def test_unavailable_single_keeps_its_previous_device(self):
        first, _ = combined([tv_group()], [button()])
        previous = previous_by_key_and_entity(first)
        down = Entity("input_button.movie_time", "Movie Time", False)
        second, _ = combined([tv_group()], [down], previous=previous)
        self.assertIn(
            "input_button.movie_time",
            {d.get("entity") for d in second["devices"]})

    def test_v2_previous_does_not_leak_into_a_new_single(self):
        # The TV was a merged device last push; now it is only a single pick
        # beside a different merged device. Its v1 commands stay bare actions.
        first, _ = combined([tv_group()], [])
        previous = previous_by_key_and_entity(first)
        other = devices.DeviceGroup(
            "dev_amp", "Amp",
            entities=[Entity("switch.amp", "Amp", True)])
        tv_single = Entity("media_player.tv", "Living Room TV", True, TV_FEATURES, {})
        device_set, _ = combined([other], [tv_single], previous=previous)
        single = next(d for d in device_set["devices"]
                      if d.get("entity") == "media_player.tv")
        self.assertNotIn("key", single)
        self.assertFalse(
            any(c["value"].startswith("media_player.tv:") for c in single["commands"]),
            single["commands"])
        assert_hub_accepts_mixed(self, device_set)


class LegacyFlattenTests(unittest.TestCase):
    def test_flatten_deduped_keeps_the_group_member_once(self):
        members = [Entity("switch.amp", "Amp (device)", True),
                   Entity("light.bias", "Bias", True)]
        groups = [devices.DeviceGroup("dev", "Amp", entities=members)]
        singles = [Entity("switch.amp", "Amp (single)", True), button(), script()]
        flat = devices.flatten_deduped(groups, singles)
        self.assertEqual(
            [e.entity_id for e in flat],
            ["switch.amp", "light.bias", "input_button.movie_time", "script.good_night"])
        self.assertIs(flat[0], members[0])

    def test_legacy_set_includes_groups_and_singles_without_duplicates(self):
        groups = [tv_group()]
        singles = [
            Entity("media_player.tv", "Living Room TV", True, TV_FEATURES, {}),
            button(), script(),
        ]
        device_set = devices.build_device_set(
            WEBHOOK, devices.flatten_deduped(groups, singles))
        entities = [d["entity"] for d in device_set["devices"]]
        self.assertEqual(
            sorted(entities),
            ["input_button.movie_time", "media_player.tv", "script.good_night"])
        allowed = devices.allowed_values(device_set)
        self.assertIn("press", allowed["input_button.movie_time"])
        self.assertIn("turn_on", allowed["script.good_night"])
        self.assertIn("turn_on", allowed["media_player.tv"])


class CombinedStatePayloadTests(unittest.TestCase):
    def test_single_entity_state_is_not_unavailable_beside_groups(self):
        lamp = Entity("switch.lamp", "Lamp", True, 0, {}, state="on")
        groups = [tv_group()]
        device_set, _ = combined(groups, [lamp])
        payload = devices.build_state_payload(groups, [lamp], device_set, 1, 100)
        by_key = {s["key"]: s for s in payload["states"]}
        self.assertEqual(by_key["switch.lamp"]["available"], True)
        self.assertEqual(by_key["switch.lamp"]["power"], "on")
        self.assertIn("media_player.tv", by_key)


if __name__ == "__main__":
    unittest.main()
