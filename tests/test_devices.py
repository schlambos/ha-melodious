"""Tests for custom_components/melodious/devices.py (the integration's pure rules).

devices.py imports nothing from Home Assistant, so it loads straight from
its file here. Run from the repo root:

    python3 -m unittest discover -s tests -p 'test_*.py'
"""

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("melodious_devices", ROOT / "custom_components/melodious/devices.py")
devices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(devices)
Entity = devices.Entity

WEBHOOK = "http://ha.example:8123/api/webhook/abc"
TV_FEATURES = (
    devices.MP_TURN_ON | devices.MP_TURN_OFF | devices.MP_VOLUME_STEP | devices.MP_VOLUME_MUTE
    | devices.MP_PLAY | devices.MP_PAUSE | devices.MP_SELECT_SOURCE
)


def tv(**attributes):
    return Entity("media_player.living_room", "Living Room TV", True, TV_FEATURES,
                  {"source_list": ["HDMI 1", "HDMI 2", "Netflix"], **attributes})


def assert_hub_accepts(test, device_set):
    """The limits codexactivity's CommitHaDevices checks (haSetFrom), in Python."""
    test.assertRegex(device_set["webhook"], r"^http://[\w.\-]+(:\d+)?/[\w\-._~/]*$")
    test.assertLessEqual(len(device_set["devices"]), 64)
    entities, names = set(), set()
    for d in device_set["devices"]:
        test.assertRegex(d["entity"], r"^[a-z0-9_]+\.[a-z0-9_]+$")
        test.assertTrue(0 < len(d["name"]) <= 64 and not re.search(r'[\x00-\x1f\x7f"\\]', d["name"]), d["name"])
        test.assertNotIn(d["entity"], entities)
        test.assertNotIn(d["name"].lower(), names)
        entities.add(d["entity"])
        names.add(d["name"].lower())
        commands = {}
        test.assertTrue(0 < len(d["commands"]) <= 128)
        for c in d["commands"]:
            test.assertRegex(c["name"], r"^[A-Za-z0-9][A-Za-z0-9 _.+\-]*$")
            test.assertLessEqual(len(c["name"]), 48)
            test.assertTrue(0 < len(c["value"]) <= 96 and not re.search(r'[\x00-\x1f\x7f"\\%{}]', c["value"]), c)
            test.assertNotIn(c["name"].lower(), commands)
            commands[c["name"].lower()] = c
        if "power" in d:
            test.assertIn(d["power"]["on"].lower(), commands)
            test.assertIn(d["power"]["off"].lower(), commands)
        test.assertLessEqual(len(d.get("inputs", [])), 32)
        for i in d.get("inputs", []):
            test.assertIn(i["command"].lower(), commands)


class DeviceSetTests(unittest.TestCase):
    def test_media_player(self):
        spec = devices.build_device_set(WEBHOOK, [tv()])["devices"][0]
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["PowerOn"], "turn_on")
        self.assertEqual(values["VolumeUp"], "volume_up")
        self.assertEqual(values["Mute"], "mute")
        self.assertEqual(values["PlayPause"], "media_play_pause")
        self.assertNotIn("Stop", values, "no STOP feature")
        self.assertEqual(spec["power"], {"on": "PowerOn", "off": "PowerOff"})
        self.assertEqual(spec["inputs"][0], {"name": "HDMI 1", "command": "Input HDMI 1"})
        self.assertEqual(values["Input Netflix"], "select_source:Netflix")

    def test_sources_the_hub_cant_carry_are_skipped(self):
        sources = ['Say "hi"', "100%", "{x}", "hdmi 1", "HDMI 1", "Pi: Kodi", "X" * 120]
        spec = devices.build_device_set(WEBHOOK, [tv(source_list=sources)])["devices"][0]
        self.assertEqual([i["name"] for i in spec["inputs"]], ["hdmi 1", "Pi: Kodi"])
        self.assertIn("Input Pi Kodi", {c["name"] for c in spec["commands"]})

    def test_input_names_come_from_the_label_and_stay_unique(self):
        spec = devices.build_device_set(WEBHOOK, [tv(source_list=["A/V", "A V", "AV"])])["devices"][0]
        self.assertEqual([i["command"] for i in spec["inputs"]], ["Input AV", "Input A V", "Input AV 2"])

    def test_other_domains(self):
        entities = [
            Entity("light.desk", "Desk", True, 0, {"supported_color_modes": ["brightness"]}),
            Entity("light.strip", "Strip", True, 0, {"supported_color_modes": ["onoff"]}),
            Entity("cover.blind", "Blind", True, devices.COVER_OPEN | devices.COVER_CLOSE, {}),
            Entity("script.movie", "Movie time", True, 0, {}),
            Entity("button.doorbell", "Doorbell", True, 0, {}),
            Entity("climate.hall", "Hall", True, 0, {}),
        ]
        by_entity = {d["entity"]: d for d in devices.build_device_set(WEBHOOK, entities)["devices"]}
        names = lambda e: [c["name"] for c in by_entity[e]["commands"]]  # noqa: E731
        self.assertEqual(names("light.desk"), ["PowerOn", "PowerOff", "PowerToggle", "BrightnessUp", "BrightnessDown"])
        self.assertEqual(names("light.strip"), ["PowerOn", "PowerOff", "PowerToggle"])
        self.assertEqual(names("cover.blind"), ["Open", "Close"])
        self.assertNotIn("power", by_entity["cover.blind"])
        self.assertEqual(names("script.movie"), ["Run"])
        self.assertEqual(names("button.doorbell"), ["Press"])
        self.assertNotIn("climate.hall", by_entity, "unsupported domain")

    def test_names_stay_unique_and_clear_of_other_hub_devices(self):
        entities = [
            Entity("switch.a", "Lamp", True), Entity("switch.b", "lamp", True),
            Entity("switch.c", "LG TV", True), Entity("switch.d", 'Bad "name"\\', True),
        ]
        names = [d["name"] for d in devices.build_device_set(WEBHOOK, entities, taken_names={"LG TV"})["devices"]]
        self.assertEqual(names, ["Lamp", "lamp 2", "LG TV (HA)", "Bad name"])

    def test_an_unavailable_entity_keeps_its_device(self):
        first = devices.build_device_set(WEBHOOK, [tv()])
        previous = {d["entity"]: d for d in first["devices"]}
        down = Entity("media_player.living_room", "Living Room TV", False)
        self.assertEqual(devices.build_device_set(WEBHOOK, [down], previous), first)
        self.assertEqual(devices.build_device_set(WEBHOOK, [down])["devices"], [], "nothing to keep yet")

    def test_a_player_switched_off_keeps_its_inputs(self):
        first = devices.build_device_set(WEBHOOK, [tv()])
        previous = {d["entity"]: d for d in first["devices"]}
        off = tv(source_list=None)
        self.assertEqual(devices.build_device_set(WEBHOOK, [off], previous), first)
        more = devices.build_device_set(WEBHOOK, [tv(source_list=["HDMI 3"])], previous)["devices"][0]
        self.assertEqual([i["name"] for i in more["inputs"]], ["HDMI 3", "HDMI 1", "HDMI 2", "Netflix"])

    def test_every_set_passes_the_hub_rules(self):
        entities = [tv(source_list=[f"Source {n}" for n in range(40)])] + [
            Entity(f"switch.s{n}", f"Switch {n % 3}", True) for n in range(70)
        ]
        device_set = devices.build_device_set(WEBHOOK, entities, taken_names={"Switch 1"})
        self.assertEqual(len(device_set["devices"]), 64)
        self.assertEqual(len(device_set["devices"][0]["inputs"]), 32)
        assert_hub_accepts(self, device_set)

    def test_allowed_values(self):
        allowed = devices.allowed_values(devices.build_device_set(WEBHOOK, [tv()]))
        self.assertIn("select_source:HDMI 2", allowed["media_player.living_room"])


class ServiceCallTests(unittest.TestCase):
    def test_calls(self):
        call = devices.service_call
        self.assertEqual(call("media_player.tv", "volume_up"), ("media_player", "volume_up", {"entity_id": "media_player.tv"}))
        self.assertEqual(call("media_player.tv", "select_source:Pi: Kodi"),
                         ("media_player", "select_source", {"entity_id": "media_player.tv", "source": "Pi: Kodi"}))
        self.assertEqual(call("media_player.tv", "mute", {"is_volume_muted": True})[2]["is_volume_muted"], False)
        self.assertEqual(call("media_player.tv", "mute", {})[2]["is_volume_muted"], True)
        self.assertEqual(call("light.desk", "brightness_down"),
                         ("light", "turn_on", {"entity_id": "light.desk", "brightness_step_pct": -10}))
        self.assertEqual(call("script.movie", "turn_on")[1], "turn_on")

    def test_anything_else_is_refused(self):
        call = devices.service_call
        self.assertIsNone(call("media_player.tv", "volume_up:5"))
        self.assertIsNone(call("light.desk", "select_source:HDMI 1"))
        self.assertIsNone(call("lock.front", "unlock"))
        self.assertIsNone(call("switch.a", "turn_off_everything"))

    def test_every_command_built_has_a_service(self):
        entities = [
            tv(), Entity("light.desk", "Desk", True, 0, {"supported_color_modes": ["brightness"]}),
            Entity("fan.f", "Fan", True, devices.FAN_SET_SPEED), Entity("scene.s", "S", True),
            Entity("cover.c", "C", True, devices.COVER_OPEN | devices.COVER_CLOSE | devices.COVER_STOP),
            Entity("input_button.b", "B", True), Entity("input_boolean.i", "I", True),
        ]
        for entity, values in devices.allowed_values(devices.build_device_set(WEBHOOK, entities)).items():
            for value in values:
                self.assertIsNotNone(devices.service_call(entity, value), f"{entity} {value}")


def group(device_id="dev1", name="Living Room TV", members=None, **device_kw):
    return devices.DeviceGroup(device_id, name, entities=members or [], **device_kw)


def mp(entity_id="media_player.living_room_tv", features=None, **attrs):
    return Entity(entity_id, "TV", True,
                  features if features is not None else TV_FEATURES,
                  {"source_list": ["HDMI 1"], **attrs})


def assert_hub_accepts_v2(test, device_set):
    """The v2 limits codexactivity's haSetFrom checks, in Python."""
    test.assertLessEqual(len(device_set["devices"]), 64)
    keys, names, entities = set(), set(), set()
    for d in device_set["devices"]:
        test.assertRegex(d["key"], r"^[a-z0-9_]+\.[a-z0-9_]+$")
        test.assertLessEqual(len(d["key"]), 128)
        test.assertNotIn(d["key"], keys)
        test.assertNotIn(d["name"].lower(), names)
        keys.add(d["key"])
        names.add(d["name"].lower())
        aliases = set()
        for item in d["entities"]:
            test.assertRegex(item["alias"], r"^[a-z0-9_.]+$")
            test.assertNotIn(":", item["alias"])
            test.assertRegex(item["entity"], r"^[a-z0-9_]+\.[a-z0-9_]+$")
            aliases.add(item["alias"])
            test.assertNotIn(item["entity"], entities, "entity in two devices")
            entities.add(item["entity"])
        test.assertTrue(1 <= len(d["entities"]) <= 32)
        test.assertTrue(0 < len(d["commands"]) <= 128)
        seen = set()
        for c in d["commands"]:
            test.assertNotIn(c["name"].lower(), seen)
            seen.add(c["name"].lower())
            alias, sep, action = c["value"].partition(":")
            test.assertTrue(sep and action, c["value"])
            test.assertIn(alias, aliases, c["value"])
            test.assertLessEqual(len(c["value"]), 96)
        if "power" in d:
            test.assertIn(d["power"]["on"].lower(), seen)
            test.assertIn(d["power"]["off"].lower(), seen)


class MergedSetTests(unittest.TestCase):
    def test_merged_device_prefixes_commands_with_aliases(self):
        g = group(members=[mp(), Entity("remote.living_room_tv", "Remote", True, 0, {})])
        merged, suppressed = devices.build_merged_set(WEBHOOK, [g], {}, {}, {})
        spec = merged["devices"][0]
        self.assertEqual(spec["key"], "media_player.living_room_tv")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["PowerOn"], "media_player.living_room_tv:turn_on")
        self.assertEqual(values["Mute"], "media_player.living_room_tv:mute")
        # The remote has no Phase-1 profile yet, but it is still in the table.
        self.assertEqual(
            spec["entities"],
            [{"alias": "media_player.living_room_tv", "entity": "media_player.living_room_tv"},
             {"alias": "remote.living_room_tv", "entity": "remote.living_room_tv"}],
        )
        self.assertEqual(spec["power"], {"on": "PowerOn", "off": "PowerOff"})
        self.assertEqual(suppressed, [])
        assert_hub_accepts_v2(self, merged)

    def test_first_command_wins_each_canonical_name(self):
        g = group(members=[mp("media_player.a"), mp("media_player.z")])
        spec = devices.build_merged_set(WEBHOOK, [g], {}, {}, {})[0]["devices"][0]
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertTrue(values["PowerOn"].startswith("media_player.a:"))
        self.assertEqual(len([c for c in spec["commands"] if c["name"] == "PowerOn"]), 1)

    def test_disabled_commands_stay_off(self):
        g = group(members=[mp()])
        spec = devices.build_merged_set(
            WEBHOOK, [g], {}, {"dev1": {"VolumeUp", "volumeup"}}, {})[0]["devices"][0]
        self.assertNotIn("VolumeUp", [c["name"] for c in spec["commands"]])

    def test_long_entity_ids_get_short_aliases(self):
        long_id = "media_player." + "x" * 90
        g = group(members=[Entity(long_id, "Long", True, TV_FEATURES, {"source_list": []})])
        spec = devices.build_merged_set(WEBHOOK, [g], {}, {}, {})[0]["devices"][0]
        self.assertRegex(spec["entities"][0]["alias"], r"^e[0-9a-f]{8}$")
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_previous_set_by_key_survives(self):
        first, _ = devices.build_merged_set(WEBHOOK, [group(members=[mp()])], {}, {}, {})
        previous = {first["devices"][0]["key"]: first["devices"][0]}
        # Player switched off: same set comes back from the merge.
        off = mp()
        off.available = False
        second, _ = devices.build_merged_set(
            WEBHOOK, [group(members=[off])], {}, {}, previous)
        self.assertEqual(second, {"webhook": WEBHOOK, "devices": [first["devices"][0]]})

    def test_key_prefers_an_entity_the_hub_already_has(self):
        old_v1 = devices.build_device_set(WEBHOOK, [mp("media_player.living_room_tv")])
        previous = {d["entity"]: d for d in old_v1["devices"]}
        g = group(members=[mp(), Entity("remote.living_room_tv", "Remote", True, 0, {})])
        merged, _ = devices.build_merged_set(WEBHOOK, [g], {}, {}, previous)
        self.assertEqual(merged["devices"][0]["key"], "media_player.living_room_tv")

    def test_cast_twin_is_suppressed(self):
        real = devices.DeviceGroup("d1", "Living Room TV", "SEI", "Box R",
                                   [mp("media_player.box", TV_FEATURES)])
        twin = devices.DeviceGroup("d2", "Living Room TV", "Google Inc.", "Chromecast",
                                   [Entity("media_player.living_room_tv_4", "LR Cast", True,
                                           TV_FEATURES, {})])
        for e in twin.entities:
            e.platform = "cast"
        for e in real.entities:
            e.platform = "androidtv_remote"
        kept, suppressed = devices.suppress_cast_twins([twin, real])
        self.assertEqual(suppressed, ["Living Room TV"])
        self.assertEqual(kept, [real])

    def test_cast_without_a_real_device_is_kept(self):
        twin = devices.DeviceGroup("d2", "Lone Cast", "Google Inc.", "Chromecast",
                                   [Entity("media_player.lone", "Lone", True, TV_FEATURES, {})])
        twin.entities[0].platform = "cast"
        kept, suppressed = devices.suppress_cast_twins([twin])
        self.assertEqual((kept, suppressed), ([twin], []))

    def test_resolve_v2_command(self):
        spec = {"entities": [{"alias": "mp", "entity": "media_player.tv"}]}
        self.assertEqual(devices.resolve_v2_command(spec, "mp:turn_on"),
                         ("media_player.tv", "turn_on"))
        self.assertEqual(devices.resolve_v2_command(spec, "mp:select_source:HDMI 1"),
                         ("media_player.tv", "select_source:HDMI 1"))
        self.assertIsNone(devices.resolve_v2_command(spec, "nope:turn_on"))
        self.assertIsNone(devices.resolve_v2_command(spec, "turn_on"))

    def test_webostv_button_service_call(self):
        self.assertEqual(
            devices.service_call("media_player.tv", "button:POWER", {}, "webostv"),
            ("webostv", "button", {"entity_id": "media_player.tv", "button": "POWER"}))

    def test_v1_sets_are_byte_identical(self):
        entities = [tv(), Entity("light.desk", "Desk", True, 0,
                                 {"supported_color_modes": ["brightness"]})]
        self.assertEqual(devices.build_device_set(WEBHOOK, entities),
                         {"webhook": WEBHOOK, "devices": [
                             {k: v for k, v in d.items()}
                             for d in devices.build_device_set(WEBHOOK, entities)["devices"]]})
        for d in devices.build_device_set(WEBHOOK, entities)["devices"]:
            self.assertIn("entity", d)
            self.assertNotIn("key", d)
            self.assertNotIn(":", [c["value"] for c in d["commands"]][0])


if __name__ == "__main__":
    unittest.main()
