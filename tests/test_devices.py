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

# The owner's real LG webOS inventory (HA 2026.1.2): 24381 has no TURN_ON.
LG_FEATURES = 24381
LG_ENTITY = "media_player.lg_webos_tv_oled65c4pua_2"
# The dump's 35 entries; the live TV may report more, so the 37-entry fixture
# below adds two more apps. Only HDMI 1, HDMI 4 and Live TV are inputs.
LG_DUMP_SOURCES = [
    "8 Ball Pool", "Always Ready", "Amazon Alexa", "Apple TV", "Apps",
    "Camera", "Copilot", "Disney+", "HBO Max", "HDMI 1", "HDMI 4",
    "Home Hub", "Hulu", "LG Channels", "LG Gaming Portal", "LG Movies & TV",
    "LG Radio+", "LG Sports Playbook", "LG Travel+", "Live TV", "Media Player",
    "Nakamichi Sound Bar", "Netflix", "Pluto TV", "Prime Video",
    "Room to Room Share", "ShopTime", "Sling TV", "Sports",
    "Switch2 Game Console", "Tubi: Free Movies & TV", "Web Browser",
    "Weekend Games - Song Quiz, Jeopardy! and CoComelon",
    "Who.Where.What?", "YouTube",
]
LG_SOURCES = LG_DUMP_SOURCES + ["Spotify", "Plex"]

# The owner's real Google TV inventory: media_player 153529, remote 4.
GOOGLE_TV_MP_FEATURES = 153529
GOOGLE_TV_REMOTE_FEATURES = 4
GOOGLE_TV_MP = "media_player.box_r_4k_plus"
GOOGLE_TV_REMOTE = "remote.box_r_4k_plus"


def tv(**attributes):
    return Entity("media_player.living_room", "Living Room TV", True, TV_FEATURES,
                  {"source_list": ["HDMI 1", "HDMI 2", "Netflix"], **attributes})


def lg_tv(**attributes):
    return Entity(
        LG_ENTITY, "LG webOS TV OLED65C4PUA", True, LG_FEATURES,
        {"source_list": LG_SOURCES, "sound_output": "external_arc",
         "volume_level": 0.12, "is_volume_muted": False, **attributes},
        platform="webostv",
    )


def google_tv(**remote_attributes):
    return [
        Entity(GOOGLE_TV_MP, "Living Room TV", True, GOOGLE_TV_MP_FEATURES,
               {"app_name": "Emby"}, platform="androidtv_remote"),
        Entity(GOOGLE_TV_REMOTE, "Living Room TV", True, GOOGLE_TV_REMOTE_FEATURES,
               {"activity_list": [], "current_activity": "tv.emby.embyatv",
                **remote_attributes}, platform="androidtv_remote"),
    ]


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

    def test_v1_webostv_keeps_the_legacy_profile(self):
        """An existing entity device must not change when the integration upgrades."""
        spec = devices.build_device_set(WEBHOOK, [lg_tv()])["devices"][0]
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["Input Netflix"], "select_source:Netflix")
        self.assertNotIn("App Netflix", values)
        self.assertNotIn("PowerToggle", values)
        self.assertNotIn("DirectionUp", values)
        self.assertNotIn("power", spec, "no TURN_ON, so the legacy pair is absent")


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

    def test_new_action_shapes_route(self):
        call = devices.service_call
        self.assertEqual(
            call("media_player.lg", "button:ENTER", {}, "webostv"),
            ("webostv", "button", {"entity_id": "media_player.lg", "button": "ENTER"}))
        self.assertEqual(
            call("remote.tv", "send_command:DPAD_UP"),
            ("remote", "send_command", {"entity_id": "remote.tv", "command": ["DPAD_UP"]}))
        self.assertEqual(
            call("remote.tv", "turn_on:Netflix"),
            ("remote", "turn_on", {"entity_id": "remote.tv", "activity": "Netflix"}))
        self.assertEqual(
            call("select.avr", "select_option:HDMI 2"),
            ("select", "select_option", {"entity_id": "select.avr", "option": "HDMI 2"}))
        self.assertIsNone(call("remote.tv", "send_command:"))
        self.assertIsNone(call("select.avr", "select_option:"))
        self.assertIsNone(call("media_player.tv", "button:ENTER", {}, None),
                          "button: is a webostv-only action")

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


def merged_spec(members, name="Living Room TV", device_id="dev1", previous=None,
                disabled=None, warnings=None):
    """One merged device from one group; (spec or None, suppressed names)."""
    device_set, suppressed = devices.build_merged_set(
        WEBHOOK, [group(device_id, name, members)], {},
        {device_id: disabled} if disabled else {}, previous or {}, warnings=warnings)
    return (device_set["devices"][0] if device_set["devices"] else None), suppressed


def assert_hub_accepts_v2(test, device_set):
    """The v2 limits codexactivity's haSetFrom checks, in Python."""
    test.assertLessEqual(len(device_set["devices"]), 64)
    keys, names, entities = set(), set(), set()
    for d in device_set["devices"]:
        test.assertRegex(d["key"], r"^[a-z0-9_]+\.[a-z0-9_]+$")
        test.assertLessEqual(len(d["key"]), 128)
        test.assertNotIn(d["key"], keys)
        test.assertNotIn(d["name"].lower(), names)
        test.assertTrue(0 < len(d["name"]) <= 64)
        test.assertFalse(re.search(r'[\x00-\x1f\x7f"\\]', d["name"]))
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
            test.assertRegex(c["name"], r"^[A-Za-z0-9][A-Za-z0-9 _.+\-]*$")
            test.assertLessEqual(len(c["name"]), 48)
            test.assertNotIn(c["name"].lower(), seen)
            seen.add(c["name"].lower())
            test.assertTrue(0 < len(c["value"]) <= 96)
            test.assertFalse(re.search(r'[\x00-\x1f\x7f"\\%{}]', c["value"]))
            alias, sep, action = c["value"].partition(":")
            test.assertTrue(sep and action, c["value"])
            test.assertIn(alias, aliases, c["value"])
        if "power" in d:
            if "toggle" in d["power"]:
                test.assertIn(d["power"]["toggle"].lower(), seen)
            else:
                test.assertIn(d["power"]["on"].lower(), seen)
                test.assertIn(d["power"]["off"].lower(), seen)
                test.assertNotEqual(d["power"]["on"].lower(), d["power"]["off"].lower())
        test.assertLessEqual(len(d.get("inputs", [])), 32)
        input_names = set()
        for i in d.get("inputs", []):
            test.assertIn(i["command"].lower(), seen)
            test.assertNotIn(i["name"].lower(), input_names)
            input_names.add(i["name"].lower())


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

    def test_lg_cast_twin_with_a_bracketed_name_is_suppressed(self):
        real = devices.DeviceGroup(
            "lg", "LG webOS TV OLED65C4PUA", "LG", "OLED65C4PUA.DUSQLJR",
            [Entity(LG_ENTITY, "LG", True, LG_FEATURES, {}, platform="webostv")])
        twin = devices.DeviceGroup(
            "lgtwin", "[LG] webOS TV OLED65C4PUA", "LGE", "OLED65C4PUA.DUSQLJR",
            [Entity("media_player.lg_webos_tv_oled65c4pua", "Cast", True, 152449, {},
                    platform="cast")])
        kept, suppressed = devices.suppress_cast_twins([twin, real])
        self.assertEqual(suppressed, ["[LG] webOS TV OLED65C4PUA"])
        self.assertEqual(kept, [real])

    def test_cast_twin_in_another_room_is_kept(self):
        real = devices.DeviceGroup(
            "lr", "Living Room TV", "SEI", "Box R 4K Plus",
            [Entity("media_player.lr", "LR", True, TV_FEATURES, {}, platform="androidtv_remote")])
        other = devices.DeviceGroup(
            "br", "Bedroom TV", "Google Inc.", "Chromecast",
            [Entity("media_player.br", "BR", True, TV_FEATURES, {}, platform="cast")])
        kept, suppressed = devices.suppress_cast_twins([real, other])
        self.assertEqual(suppressed, [])
        self.assertEqual(kept, [real, other])

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
        self.assertEqual(devices.resolve_v2_command(spec, "mp:send_command:DPAD_UP"),
                         ("media_player.tv", "send_command:DPAD_UP"))
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


class LGWebOSTests(unittest.TestCase):
    def test_command_table_from_the_owners_features(self):
        spec, suppressed = merged_spec([lg_tv()], name="LG webOS TV OLED65C4PUA",
                                       device_id="lg")
        self.assertEqual(suppressed, [])
        alias = spec["entities"][0]["alias"]
        # A long app value forces the stable short alias, so every value fits 96.
        self.assertRegex(alias, r"^e[0-9a-f]{8}$")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertNotIn("PowerOn", values, "24381 has no TURN_ON")
        self.assertEqual(values["PowerOff"], f"{alias}:turn_off")
        self.assertEqual(values["PowerToggle"], f"{alias}:button:POWER")
        self.assertEqual(spec["power"], {"toggle": "PowerToggle"})
        self.assertEqual(values["VolumeUp"], f"{alias}:volume_up")
        self.assertEqual(values["VolumeDown"], f"{alias}:volume_down")
        self.assertEqual(values["Mute"], f"{alias}:mute")
        for name in ("Play", "Pause", "PlayPause", "Stop", "SkipForward", "SkipBack"):
            self.assertIn(name, values)
        expected_buttons = {
            "DirectionUp": "UP", "DirectionDown": "DOWN",
            "DirectionLeft": "LEFT", "DirectionRight": "RIGHT",
            "Select": "ENTER", "Back": "BACK", "Home": "HOME", "Menu": "MENU",
            "Guide": "GUIDE", "Info": "INFO", "Exit": "EXIT",
            "Dash": "DASH", "Record": "RECORD",
            "Red": "RED", "Green": "GREEN", "Yellow": "YELLOW", "Blue": "BLUE",
            "ChannelUp": "CHANNELUP", "ChannelDown": "CHANNELDOWN",
        }
        for name, button in expected_buttons.items():
            self.assertEqual(values[name], f"{alias}:button:{button}", name)
        for digit in "0123456789":
            self.assertEqual(values[digit], f"{alias}:button:{digit}")
        self.assertEqual(len(spec["commands"]), 77)
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_input_app_split_for_the_37_entry_source_list(self):
        spec, _ = merged_spec([lg_tv()], name="LG webOS TV OLED65C4PUA", device_id="lg")
        alias = spec["entities"][0]["alias"]
        self.assertEqual([i["name"] for i in spec["inputs"]], ["HDMI 1", "HDMI 4", "Live TV"])
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["Input HDMI 1"], f"{alias}:select_source:HDMI 1")
        self.assertEqual(values["Input HDMI 4"], f"{alias}:select_source:HDMI 4")
        self.assertEqual(values["Input Live TV"], f"{alias}:select_source:Live TV")
        self.assertNotIn("Input Netflix", values)
        self.assertEqual(values["App Netflix"], f"{alias}:select_source:Netflix")
        apps = [c["name"] for c in spec["commands"] if c["name"].startswith("App ")]
        self.assertEqual(len(apps), 34)
        self.assertEqual(len(spec["inputs"]) + len(apps), 37)

    def test_input_app_split_for_the_dumps_35_entries(self):
        spec, _ = merged_spec([lg_tv(source_list=LG_DUMP_SOURCES)],
                              name="LG webOS TV OLED65C4PUA", device_id="lg")
        self.assertEqual([i["name"] for i in spec["inputs"]], ["HDMI 1", "HDMI 4", "Live TV"])
        self.assertEqual(
            len([c for c in spec["commands"] if c["name"].startswith("App ")]), 32)

    def test_command_order_for_the_touch_hard_keys(self):
        spec, _ = merged_spec([lg_tv()], name="LG webOS TV OLED65C4PUA", device_id="lg")
        names = [c["name"] for c in spec["commands"]]
        self.assertLess(names.index("Menu"), names.index("Home"))
        self.assertLess(names.index("Back"), names.index("SkipBack"))
        self.assertIn("Select", names)
        self.assertNotIn("Enter", names)
        self.assertLess(names.index("Input HDMI 1"), names.index("App Netflix"))

    def test_both_power_bits_use_discrete_power(self):
        entity = lg_tv()
        entity.supported_features = LG_FEATURES | devices.MP_TURN_ON
        spec, _ = merged_spec([entity], name="LG webOS TV OLED65C4PUA", device_id="lg")
        alias = spec["entities"][0]["alias"]
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["PowerOn"], f"{alias}:turn_on")
        self.assertNotIn("PowerToggle", values)
        self.assertEqual(spec["power"], {"on": "PowerOn", "off": "PowerOff"})

    def test_disabling_power_toggle_drops_the_power_pair(self):
        spec, _ = merged_spec([lg_tv()], name="LG webOS TV OLED65C4PUA",
                              device_id="lg", disabled={"PowerToggle"})
        self.assertNotIn("power", spec)
        self.assertNotIn("PowerToggle", [c["name"] for c in spec["commands"]])

    def test_volume_set_without_step_uses_the_tv_buttons(self):
        entity = lg_tv()
        entity.supported_features = (LG_FEATURES & ~devices.MP_VOLUME_STEP) | devices.MP_VOLUME_SET
        spec, _ = merged_spec([entity], name="LG webOS TV OLED65C4PUA", device_id="lg")
        alias = spec["entities"][0]["alias"]
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["VolumeUp"], f"{alias}:button:VOLUMEUP")
        self.assertEqual(values["VolumeDown"], f"{alias}:button:VOLUMEDOWN")

    def test_v1_previous_commands_migrate_to_v2_aliases(self):
        entity = lg_tv(source_list=["HDMI 1", "Netflix"])
        old = devices.build_device_set(WEBHOOK, [entity])
        previous = {d["entity"]: d for d in old["devices"]}
        spec, _ = merged_spec([entity], name="LG webOS TV OLED65C4PUA",
                              device_id="lg", previous=previous)
        aliases = {i["alias"] for i in spec["entities"]}
        for c in spec["commands"]:
            alias, sep, action = c["value"].partition(":")
            self.assertTrue(sep and action, c)
            self.assertIn(alias, aliases, c)
        values = {c["name"]: c["value"] for c in spec["commands"]}
        alias = spec["entities"][0]["alias"]
        self.assertEqual(values["Input Netflix"], f"{alias}:select_source:Netflix")
        self.assertEqual(values["PowerOff"], f"{alias}:turn_off")
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_v1_previous_long_source_migrates_to_the_short_alias(self):
        entity = lg_tv(source_list=[
            "Weekend Games - Song Quiz, Jeopardy! and CoComelon", "HDMI 1"])
        old = devices.build_device_set(WEBHOOK, [entity])
        previous = {d["entity"]: d for d in old["devices"]}
        spec, _ = merged_spec([entity], name="LG webOS TV OLED65C4PUA",
                              device_id="lg", previous=previous)
        alias = spec["entities"][0]["alias"]
        self.assertRegex(alias, r"^e[0-9a-f]{8}$")
        old_input = next(c for c in spec["commands"]
                         if c["name"].startswith("Input Weekend Games"))
        self.assertTrue(old_input["value"].startswith(f"{alias}:select_source:"))
        self.assertLessEqual(len(old_input["value"]), 96)
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_128_command_trim_keeps_controls_and_reports_apps(self):
        sources = ["HDMI 1", "HDMI 4", "Live TV"] + [f"Streaming {n}" for n in range(200)]
        warnings = []
        spec, _ = merged_spec([lg_tv(source_list=sources)],
                              name="LG webOS TV OLED65C4PUA", device_id="lg",
                              warnings=warnings)
        self.assertEqual(len(spec["commands"]), 128)
        self.assertEqual(len(spec["inputs"]), 3)
        self.assertEqual(
            sum(1 for c in spec["commands"] if c["name"].startswith("App ")), 85)
        self.assertEqual(len(warnings), 1)
        self.assertIn("243 commands", warnings[0])
        self.assertIn("kept 128", warnings[0])
        self.assertIn("115 app commands", warnings[0])
        names = [c["name"] for c in spec["commands"]]
        for name in ("PowerOff", "PowerToggle", "VolumeUp", "DirectionUp",
                     "Select", "Menu", "Home", "Input HDMI 1"):
            self.assertIn(name, names)
        self.assertTrue(spec["commands"][-1]["name"].startswith("App "))
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})


class GoogleTVTests(unittest.TestCase):
    def test_command_table_from_the_owners_features(self):
        spec, suppressed = merged_spec(google_tv(), name="Living Room TV", device_id="gt")
        self.assertEqual(suppressed, [])
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["PowerOn"], f"{GOOGLE_TV_MP}:turn_on")
        self.assertEqual(values["PowerOff"], f"{GOOGLE_TV_MP}:turn_off")
        self.assertEqual(spec["power"], {"on": "PowerOn", "off": "PowerOff"})
        for name in ("VolumeUp", "VolumeDown", "Mute", "Play", "Pause",
                     "PlayPause", "Stop", "SkipForward", "SkipBack"):
            self.assertTrue(values[name].startswith(GOOGLE_TV_MP + ":"), name)
        expected = {
            "DirectionUp": "DPAD_UP", "DirectionDown": "DPAD_DOWN",
            "DirectionLeft": "DPAD_LEFT", "DirectionRight": "DPAD_RIGHT",
            "Select": "DPAD_CENTER", "Back": "BACK", "Menu": "MENU", "Home": "HOME",
            "Dash": "MINUS", "Red": "PROG_RED", "Green": "PROG_GREEN",
            "Yellow": "PROG_YELLOW", "Blue": "PROG_BLUE",
        }
        for name, key in expected.items():
            self.assertEqual(values[name], f"{GOOGLE_TV_REMOTE}:send_command:{key}", name)
        for digit in "0123456789":
            self.assertEqual(values[digit], f"{GOOGLE_TV_REMOTE}:send_command:{digit}")
        self.assertNotIn("Record", values, "no RECORD key in androidtvremote2 0.2.3")
        self.assertNotIn("inputs", spec)
        self.assertFalse([c for c in spec["commands"] if c["name"].startswith("App ")])
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_empty_activity_list_means_no_app_commands(self):
        spec, _ = merged_spec(google_tv(), name="Living Room TV", device_id="gt")
        self.assertFalse([c for c in spec["commands"] if c["name"].startswith("App ")])

    def test_activity_list_becomes_app_commands(self):
        spec, _ = merged_spec(google_tv(activity_list=["Netflix", "YouTube"]),
                              name="Living Room TV", device_id="gt")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["App Netflix"], f"{GOOGLE_TV_REMOTE}:turn_on:Netflix")
        self.assertEqual(values["App YouTube"], f"{GOOGLE_TV_REMOTE}:turn_on:YouTube")
        self.assertEqual([c["name"] for c in spec["commands"]][-1], "App YouTube")

    def test_command_order_for_the_touch_hard_keys(self):
        spec, _ = merged_spec(google_tv(), name="Living Room TV", device_id="gt")
        names = [c["name"] for c in spec["commands"]]
        self.assertLess(names.index("Menu"), names.index("Home"))
        self.assertIn("Select", names)
        self.assertNotIn("Enter", names)

    def test_disabling_power_off_drops_the_power_pair(self):
        spec, _ = merged_spec(google_tv(), name="Living Room TV", device_id="gt",
                              disabled={"PowerOff"})
        self.assertNotIn("power", spec)
        self.assertNotIn("PowerOff", [c["name"] for c in spec["commands"]])


class AndroidTVADBTests(unittest.TestCase):
    def test_adb_key_table_and_deliberate_gaps(self):
        members = [
            Entity("media_player.shield", "Shield", True, TV_FEATURES, {},
                   platform="androidtv"),
            Entity("remote.shield", "Shield", True, 0, {}, platform="androidtv"),
        ]
        spec, _ = merged_spec(members, name="Shield", device_id="adb")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        expected = {
            "DirectionUp": "UP", "DirectionDown": "DOWN",
            "DirectionLeft": "LEFT", "DirectionRight": "RIGHT",
            "Select": "ENTER", "Back": "BACK", "Menu": "MENU", "Home": "HOME",
            "Red": "RED", "Green": "GREEN", "Yellow": "YELLOW", "Blue": "BLUE",
        }
        for name, key in expected.items():
            self.assertEqual(values[name], f"remote.shield:send_command:{key}", name)
        for name in ("0", "Dash", "Record", "Guide", "Info", "Exit",
                     "ChannelUp", "ChannelDown"):
            self.assertNotIn(name, values, f"{name} is not in androidtv.constants.KEYS")
        self.assertEqual(values["PowerOn"], "media_player.shield:turn_on")
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_unknown_remote_platform_is_ignored(self):
        spec, _ = merged_spec(
            [Entity("remote.harmony", "Harmony", True, 0, {}, platform="harmony")],
            name="Harmony", device_id="h")
        self.assertIsNone(spec)


class SelectTests(unittest.TestCase):
    def test_options_become_inputs(self):
        entity = Entity("select.avr_input", "AVR input", True, 0,
                        {"options": ["HDMI 1", "HDMI 2", "TV", "Phono"]},
                        platform="template")
        spec, _ = merged_spec([entity], name="AVR", device_id="avr")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual([i["name"] for i in spec["inputs"]],
                         ["HDMI 1", "HDMI 2", "TV", "Phono"])
        for option in ("HDMI 1", "HDMI 2", "TV", "Phono"):
            self.assertEqual(values[f"Input {option}"],
                             f"select.avr_input:select_option:{option}")
        self.assertNotIn("power", spec)
        assert_hub_accepts_v2(self, {"webhook": WEBHOOK, "devices": [spec]})

    def test_unsafe_options_are_skipped(self):
        entity = Entity("select.avr", "AVR", True, 0,
                        {"options": ['HDMI "1"', "100%", "HDMI 2"]})
        spec, _ = merged_spec([entity], name="AVR", device_id="avr")
        self.assertEqual([i["name"] for i in spec["inputs"]], ["HDMI 2"])

    def test_an_option_with_a_colon_keeps_its_value(self):
        entity = Entity("select.avr", "AVR", True, 0, {"options": ["Input: TV"]})
        spec, _ = merged_spec([entity], name="AVR", device_id="avr")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertEqual(values["Input Input TV"], "select.avr:select_option:Input: TV")
        self.assertEqual(devices.service_call("select.avr", "select_option:Input: TV"),
                         ("select", "select_option",
                          {"entity_id": "select.avr", "option": "Input: TV"}))


class CastProfileTests(unittest.TestCase):
    def test_cast_sources_are_apps_not_inputs(self):
        entity = Entity("media_player.cast", "Cast", True, TV_FEATURES,
                        {"source_list": ["Netflix", "Spotify"]}, platform="cast")
        spec, _ = merged_spec([entity], name="Cast", device_id="cast")
        values = {c["name"]: c["value"] for c in spec["commands"]}
        self.assertNotIn("inputs", spec)
        self.assertEqual(values["App Netflix"], "media_player.cast:select_source:Netflix")
        self.assertEqual(values["App Spotify"], "media_player.cast:select_source:Spotify")


class NewShapeCoverageTests(unittest.TestCase):
    def test_allowed_values_cover_every_new_shape(self):
        groups = [
            group("lg", "LG webOS TV OLED65C4PUA", [lg_tv()]),
            group("gt", "Living Room TV", google_tv()),
            group("avr", "AVR", [Entity("select.avr_input", "AVR input", True, 0,
                                        {"options": ["HDMI 1"]}, platform="template")]),
        ]
        device_set, _ = devices.build_merged_set(WEBHOOK, groups, {}, {}, {})
        allowed = devices.allowed_values(device_set)
        by_key = {d["key"]: d for d in device_set["devices"]}
        lg_alias = by_key[LG_ENTITY]["entities"][0]["alias"]
        gt_remote_alias = next(
            i["alias"] for i in by_key[GOOGLE_TV_MP]["entities"]
            if i["entity"] == GOOGLE_TV_REMOTE)
        avr_alias = by_key["select.avr_input"]["entities"][0]["alias"]
        self.assertIn(f"{lg_alias}:button:POWER", allowed[LG_ENTITY])
        self.assertIn(f"{gt_remote_alias}:send_command:DPAD_UP", allowed[GOOGLE_TV_MP])
        self.assertIn(f"{avr_alias}:select_option:HDMI 1", allowed["select.avr_input"])

    def test_every_v2_command_has_a_service(self):
        members = [lg_tv(), *google_tv(),
                   Entity("select.avr_input", "AVR input", True, 0,
                          {"options": ["HDMI 1"]}, platform="template"),
                   Entity("light.desk", "Desk", True, 0,
                          {"supported_color_modes": ["brightness"]})]
        device_set, _ = devices.build_merged_set(
            WEBHOOK, [group("all", "All", members)], {}, {}, {})
        platforms = {e.entity_id: e.platform for e in members}
        for d in device_set["devices"]:
            table = {i["alias"]: i["entity"] for i in d["entities"]}
            for c in d["commands"]:
                alias, _, action = c["value"].partition(":")
                entity_id = table[alias]
                self.assertIsNotNone(
                    devices.service_call(entity_id, action, {}, platforms.get(entity_id)),
                    f"{entity_id} {action}",
                )

    def test_command_names_are_canonical_not_integration_vocabulary(self):
        members = [lg_tv(), *google_tv()]
        device_set, _ = devices.build_merged_set(
            WEBHOOK, [group("all", "All", members)], {}, {}, {})
        for d in device_set["devices"]:
            for c in d["commands"]:
                self.assertNotRegex(
                    c["name"], r"^(DPAD_|KEYCODE_|send_command|button|select_option|turn_on)")
                self.assertNotIn(c["name"], ("ENTER", "DPAD_CENTER", "KEYCODE_DPAD_CENTER"))


class HubSeedTests(unittest.TestCase):
    def test_commands_are_read_beside_the_device(self):
        config = {"deviceList": {"DevicesWithFeatures": [
            {"Device": {"Name": "Amp", "AutoDetectedDevice": {"Id": "melodious-ha-1"},
                        "Tokens": {"Parameters": [{"Key": "{d}", "Value": "media_player.amp"}]}},
             "Commands": [{"Name": "SkipForward", "Parameters": [{"Key": "{c}", "Value": "media_next_track"}]}]},
            {"Device": {"Name": "IR TV", "AutoDetectedDevice": None}, "Commands": [{"Name": "PowerOn"}]},
        ]}}
        self.assertEqual(devices.hub_ha_devices(config), [{
            "entity": "media_player.amp", "name": "Amp",
            "commands": [{"name": "SkipForward", "value": "media_next_track"}],
        }])


class StateSnapshotTests(unittest.TestCase):
    def test_lg_webos_input_vs_app_and_volume(self):
        ent = lg_tv(source="HDMI 1", volume_level=0.35, is_volume_muted=False)
        ent.state = "on"
        snap = devices.device_state_snapshot(LG_ENTITY, [ent], seq=7, updated=1762500000)
        self.assertEqual(snap, {
            "key": LG_ENTITY,
            "available": True,
            "power": "on",
            "source": "HDMI 1",
            "volume": 0.35,
            "muted": False,
            "playing": "idle",
            "activity": None,
            "seq": 7,
            "updated": 1762500000,
            "entities": {LG_ENTITY: "on"},
        })

        app_ent = lg_tv(source="Netflix", volume_level=0.2, is_volume_muted=True)
        app_ent.state = "playing"
        app_snap = devices.device_state_snapshot(
            LG_ENTITY, [app_ent], seq=8, updated=1762500005, aliases={LG_ENTITY: "e12345678"}
        )
        self.assertEqual(app_snap["power"], "on")
        self.assertIsNone(app_snap["source"])
        self.assertEqual(app_snap["activity"], "Netflix")
        self.assertEqual(app_snap["playing"], "playing")
        self.assertEqual(app_snap["muted"], True)
        self.assertEqual(app_snap["entities"], {"e12345678": "playing"})

    def test_google_tv_merged_entities_snapshot(self):
        mp_ent, rem_ent = google_tv()
        mp_ent.state = "playing"
        mp_ent.attributes.update({"volume_level": 0.5, "is_volume_muted": False})
        rem_ent.state = "on"
        snap = devices.device_state_snapshot(
            GOOGLE_TV_MP, [mp_ent, rem_ent], seq=12, updated=1762500010
        )
        self.assertEqual(snap["available"], True)
        self.assertEqual(snap["power"], "on")
        self.assertIsNone(snap["source"])
        self.assertEqual(snap["activity"], "tv.emby.embyatv")
        self.assertEqual(snap["playing"], "playing")
        self.assertEqual(snap["volume"], 0.5)
        self.assertEqual(snap["muted"], False)
        self.assertEqual(
            snap["entities"],
            {GOOGLE_TV_MP: "playing", GOOGLE_TV_REMOTE: "on"},
        )

    def test_standby_maps_to_off_and_idle_paused_map_to_on(self):
        ent = mp()
        ent.state = "standby"
        snap = devices.device_state_snapshot(ent.entity_id, [ent], seq=1, updated=100)
        self.assertEqual(snap["power"], "off")
        self.assertEqual(snap["playing"], "off")

        for active in ("idle", "playing", "paused", "on"):
            ent.state = active
            snap = devices.device_state_snapshot(ent.entity_id, [ent], seq=2, updated=101)
            self.assertEqual(snap["power"], "on")

    def test_unavailable_or_missing_never_invents_off(self):
        down = Entity("media_player.tv", "TV", available=False, state="unavailable",
                      attributes={"source": "HDMI 1", "volume_level": 0.4, "is_volume_muted": False})
        snap = devices.device_state_snapshot("media_player.tv", [down], seq=3, updated=200)
        self.assertEqual(snap["available"], False)
        self.assertEqual(snap["power"], "unknown")
        self.assertEqual(snap["playing"], "unknown")
        self.assertIsNone(snap["source"])
        self.assertIsNone(snap["volume"])
        self.assertIsNone(snap["muted"])
        self.assertIsNone(snap["activity"])
        self.assertEqual(snap["entities"], {"media_player.tv": "unavailable"})

    def test_select_entity_populates_source(self):
        sel = Entity("select.avr_input", "AVR input", available=True, state="HDMI 2",
                     attributes={"options": ["HDMI 1", "HDMI 2"], "current_option": "HDMI 2"})
        snap = devices.device_state_snapshot("select.avr_input", [sel], seq=4, updated=300)
        self.assertEqual(snap["available"], True)
        self.assertEqual(snap["power"], "unknown")
        self.assertEqual(snap["source"], "HDMI 2")

    def test_build_state_payload_batches_merged_and_v1_sets(self):
        lg = lg_tv(source="HDMI 4")
        lg.state = "on"
        gt_mp, gt_rem = google_tv()
        gt_mp.state = "off"
        gt_rem.state = "off"
        groups = [
            group("lg", "LG webOS TV OLED65C4PUA", [lg]),
            group("gt", "Living Room TV", [gt_mp, gt_rem]),
        ]
        device_set, _ = devices.build_merged_set(WEBHOOK, groups, {}, {}, {})
        payload = devices.build_state_payload(groups, [], device_set, seq=9, updated=400)
        by_key = {s["key"]: s for s in payload["states"]}
        self.assertEqual(by_key[LG_ENTITY]["power"], "on")
        self.assertEqual(by_key[LG_ENTITY]["source"], "HDMI 4")
        self.assertEqual(by_key[GOOGLE_TV_MP]["power"], "off")
        self.assertEqual(by_key[GOOGLE_TV_MP]["playing"], "off")
        self.assertEqual(by_key[GOOGLE_TV_MP]["seq"], 9)


class StableAliasTests(unittest.TestCase):
    def test_a_tv_that_is_off_keeps_its_short_alias(self):
        long_app = "Weekend Games - Song Quiz, Jeopardy! and CoComelon"
        def lg(sources):
            entity = Entity("media_player.lg_webos_tv_oled65c4pua_2", "LG", True, 24509,
                            {"source_list": sources}, platform="webostv")
            return devices.DeviceGroup("lg", "LG TV", "LG", "OLED", [entity])
        on = devices.merge_device(lg(["HDMI 1", long_app]), key="media_player.lg_webos_tv_oled65c4pua_2")
        alias = on["entities"][0]["alias"]
        self.assertTrue(alias.startswith("e"), alias)
        off = devices.merge_device(lg([]), key="media_player.lg_webos_tv_oled65c4pua_2", previous=on)
        self.assertEqual(off["entities"][0]["alias"], alias)
        values = {c["name"]: c["value"] for c in off["commands"]}
        self.assertIn(long_app, values["App Weekend Games - Song Quiz Jeopardy and C"])
        self.assertTrue(all(len(v) <= devices.MAX_VALUE for v in values.values()))


if __name__ == "__main__":
    unittest.main()
