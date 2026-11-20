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
        test.assertTrue(0 < len(d["name"].encode("utf-8")) <= 64, d["name"])
        test.assertFalse(re.search(r'[\x00-\x1f\x7f"\\]', d["name"]), d["name"])
        names.add(d["name"].lower())
        if "model" in d and d["model"] is not None:
            test.assertTrue(0 < len(d["model"].encode("utf-8")) <= 64, d["model"])
            test.assertFalse(re.search(r'[\x00-\x1f\x7f"\\]', d["model"]), d["model"])
        if d.get("key") or d.get("entities") is not None:
            test.assertTrue(1 <= len(d["entities"]) <= 32, len(d["entities"]))
            aliases = set()
            for item in d["entities"]:
                test.assertTrue(0 < len(item["alias"]) <= 64, item["alias"])
                test.assertRegex(item["alias"], r"^[a-z0-9_.]+$")
                aliases.add(item["alias"])
            for c in d["commands"]:
                alias, sep, action = c["value"].partition(":")
                test.assertTrue(sep and action, c["value"])
                test.assertIn(alias, aliases, c["value"])
        members = [i["entity"] for i in d.get("entities") or []] or [d["entity"]]
        for entity_id in members:
            test.assertNotIn(entity_id, entities, f"{entity_id} in two devices")
            entities.add(entity_id)
        test.assertTrue(0 < len(d["commands"]) <= devices.MAX_COMMANDS)
        for c in d["commands"]:
            test.assertRegex(c["name"], r"^[A-Za-z0-9][A-Za-z0-9 _.+\-]*$", c["name"])
            test.assertLessEqual(len(c["name"].encode("utf-8")), 48, c["name"])
            test.assertTrue(0 < len(c["value"].encode("utf-8")) <= devices.MAX_VALUE, c)
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

    def test_hue_model_with_quotes_or_long_name_is_cleaned_for_hub(self):
        # Hue lamps/lightstrips carry inch quotes in device.model (e.g. 65" or 5/6").
        # codexactivity rejects quotes/backslashes/>64 chars in model with HTTP 422,
        # which blocks the entire batch including single scripts pushed beside it.
        hue = devices.DeviceGroup(
            "dev_hue_strip",
            "TV Backlight",
            manufacturer="Signify Netherlands B.V.",
            model='Hue Play gradient lightstrip 65" (LCX002) ' + "x" * 40,
            entities=[Entity("light.tv_backlight", "TV Backlight", True, 0,
                             {"supported_color_modes": ["xy"]})],
        )
        device_set, _ = combined([hue], [script()])
        assert_hub_accepts_mixed(self, device_set)
        self.assertIn("script.good_night", {d.get("entity") for d in device_set["devices"]})

    def test_hue_room_with_many_scenes_sensors_and_long_ids_respects_hub_limits(self):
        # A Hue Room/Zone can have >32 scenes/lights plus unsupported sensors/events
        # and >64-char entity IDs. codexactivity refuses >32 entities or >64-char
        # aliases with HTTP 422, blocking single entities in the same push.
        long_sensor = "sensor." + "hue_living_room_zigbee_connectivity_status_" + "a" * 35
        long_remote = "remote." + "hue_living_room_unsupported_remote_helper_" + "b" * 35
        members = [
            Entity(long_sensor, "Connectivity", True),
            Entity(long_remote, "Remote", True),
            Entity("light.living_room_hue", "Living Room", True, 0,
                   {"supported_color_modes": ["xy"]}),
            *[Entity(f"scene.living_room_hue_scene_{i:02d}", f"Scene {i}", True) for i in range(40)],
        ]
        hue_room = devices.DeviceGroup("dev_hue_room", "Living Room Hue", entities=members)
        device_set, _ = combined([hue_room], [script()])
        assert_hub_accepts_mixed(self, device_set)
        merged = next(d for d in device_set["devices"] if d.get("key"))
        # Fallback key skips unsupported sensor.* and picks a supported entity.
        self.assertNotEqual(merged["key"], long_sensor)
        # Unsupported sensor.* is excluded from entities table.
        self.assertNotIn(long_sensor, {item["entity"] for item in merged["entities"]})
        self.assertIn("script.good_night", {d.get("entity") for d in device_set["devices"]})

    def test_recreated_hub_seed_of_v2_merged_device_never_double_prefixes(self):
        # After deleting and recreating the hub entry in HA, _hub_seed comes from
        # hub_ha_devices(/api/activity-config). Existing v2 devices already carry
        # "<alias>:<action>" values and must not be double-prefixed or mistaken
        # for v1 single devices.
        config = {"deviceList": {"DevicesWithFeatures": [{
            "Device": {
                "Name": "Living Room Hue",
                "AutoDetectedDevice": {"Id": "melodious-ha-1"},
                "Tokens": {"Parameters": [{"Key": "{d}", "Value": "light.hue_living_room"}]},
            },
            "Commands": [
                {"Name": "PowerOn", "Parameters": [{"Key": "{c}", "Value": "light.hue_living_room:turn_on"}]},
                {"Name": "PowerOff", "Parameters": [{"Key": "{c}", "Value": "light.hue_living_room:turn_off"}]},
                {"Name": "BrightnessUp", "Parameters": [{"Key": "{c}", "Value": "light.hue_living_room:brightness_up"}]},
                {"Name": "Activate", "Parameters": [{"Key": "{c}", "Value": "scene.hue_relax:turn_on"}]},
            ],
        }]}}
        seeded = devices.hub_ha_devices(config)
        previous = previous_by_key_and_entity({"devices": seeded})
        # Light now reports onoff-only (e.g. while unavailable/restarting) so
        # BrightnessUp comes from previous; scene.hue_relax is still on the device.
        hue = devices.DeviceGroup(
            "dev_hue", "Living Room Hue",
            entities=[
                Entity("light.hue_living_room", "Living Room", True, 0,
                       {"supported_color_modes": ["onoff"]}),
                Entity("scene.hue_relax", "Relax", True),
            ],
        )
        device_set, _ = combined([hue], [script()], previous=previous)
        assert_hub_accepts_mixed(self, device_set)
        merged = next(d for d in device_set["devices"] if d.get("key") == "light.hue_living_room")
        values = {c["name"]: c["value"] for c in merged["commands"]}
        self.assertEqual(values["BrightnessUp"], "light.hue_living_room:brightness_up")
        self.assertEqual(values["Activate"], "scene.hue_relax:turn_on")
        self.assertFalse(any(v.count(":") > 1 for v in values.values()), values)

    def test_hue_room_key_prefers_light_regardless_of_registry_order(self):
        # A Hue room's entity registry order can put scenes or unsupported
        # remotes ahead of light.salon. Picking by entity priority keeps the
        # room's key anchored on light.salon across scene edits or reorders.
        members_scene_first = [
            Entity("scene.salon_detente", "Salon Détente", True),
            Entity("scene.salon_lecture", "Salon Lecture", True),
            Entity("remote.salon_unknown", "Salon Remote", True),
            Entity("light.salon", "Salon", True, 0, {"supported_color_modes": ["xy"]}),
        ]
        members_light_first = list(reversed(members_scene_first))
        set_a, _ = combined([devices.DeviceGroup("dev_salon", "Salon", entities=members_scene_first)], [script()])
        set_b, _ = combined([devices.DeviceGroup("dev_salon", "Salon", entities=members_light_first)], [script()])
        key_a = next(d["key"] for d in set_a["devices"] if d.get("key"))
        key_b = next(d["key"] for d in set_b["devices"] if d.get("key"))
        self.assertEqual(key_a, "light.salon")
        self.assertEqual(key_b, "light.salon")
        assert_hub_accepts_mixed(self, set_a)

    def test_hue_room_upgrade_preserves_existing_key_entity_under_32_cap(self):
        # An existing Hue room pushed under v0.7.2 may have its hub key set to
        # a scene beyond the alphabetical 32-entity cap. Upgrading must keep
        # that key AND keep that entity inside entities so the hub matches it.
        key_scene = "scene.salon_z_detente"
        previous = previous_by_key_and_entity({"devices": [{
            "key": key_scene,
            "name": "Salon",
            "entities": [
                {"alias": "light.salon", "entity": "light.salon"},
                {"alias": key_scene, "entity": key_scene},
            ],
            "commands": [
                {"name": "PowerOn", "value": "light.salon:turn_on"},
                {"name": "PowerOff", "value": "light.salon:turn_off"},
                {"name": "Activate", "value": "scene.salon_00:turn_on"},
            ],
        }]})
        members = [
            Entity("light.salon", "Salon", True, 0, {"supported_color_modes": ["xy"]}),
            *[Entity(f"scene.salon_{i:02d}", f"Scène {i}", True) for i in range(36)],
            Entity(key_scene, "Salon Détente", True),
        ]
        salon = devices.DeviceGroup("dev_salon", "Salon", entities=members)
        device_set, _ = combined([salon], [script()], previous=previous)
        assert_hub_accepts_mixed(self, device_set)
        merged = next(d for d in device_set["devices"] if d.get("key"))
        self.assertEqual(merged["key"], key_scene)
        self.assertIn(key_scene, {item["entity"] for item in merged["entities"]})

    def test_key_entity_survives_32_entity_cap_with_32_other_active_contributors(self):
        # Reviewer finding: when 33 selects each contribute a distinct option
        # and the prior key is the 33rd (select.s32), reserving the key slot
        # must keep select.s32 inside the 32-entity cap whether it is available
        # or temporarily unavailable.
        key_entity = "select.s32"
        previous = previous_by_key_and_entity({"devices": [{
            "key": key_entity,
            "name": "Sources",
            "entities": [{"alias": key_entity, "entity": key_entity}],
            "commands": [{"name": "Input Opt 32", "value": f"{key_entity}:select_option:Opt 32"}],
        }]})
        for key_available in (True, False):
            members = [
                Entity(f"select.s{i:02d}", f"S{i}", True if i < 32 else key_available,
                       0, {"options": [f"Opt {i:02d}"]})
                for i in range(33)
            ]
            group = devices.DeviceGroup("dev_sources", "Sources", entities=members)
            device_set, _ = combined([group], [], previous=previous)
            assert_hub_accepts_mixed(self, device_set)
            merged = next(d for d in device_set["devices"] if d.get("key"))
            self.assertEqual(merged["key"], key_entity, f"available={key_available}")
            self.assertEqual(len(merged["entities"]), 32, f"available={key_available}")
            self.assertIn(key_entity, {item["entity"] for item in merged["entities"]},
                          f"available={key_available}")

    def test_recreated_hub_seed_matches_short_aliased_v2_device(self):
        # If a v2 device on the hub had commands using a short alias (for a
        # >64-char entity ID) and a legacy key, hub_ha_devices() cannot spell
        # the long entity ID from the alias alone; build_merged_set must still
        # match via _short_alias so the hub key does not drift on recreate.
        long_light = "light." + "salon_plafonnier_principal_couleur_ambiance_" + "a" * 30
        alias = devices._short_alias(long_light)
        config = {"deviceList": {"DevicesWithFeatures": [{
            "Device": {
                "Name": "Salon",
                "AutoDetectedDevice": {"Id": "melodious-ha-1"},
                "Tokens": {"Parameters": [{"Key": "{d}", "Value": "scene.salon_legacy_key"}]},
            },
            "Commands": [
                {"Name": "PowerOn", "Parameters": [{"Key": "{c}", "Value": f"{alias}:turn_on"}]},
                {"Name": "PowerOff", "Parameters": [{"Key": "{c}", "Value": f"{alias}:turn_off"}]},
            ],
        }]}}
        seeded = devices.hub_ha_devices(config)
        previous = previous_by_key_and_entity({"devices": seeded})
        salon = devices.DeviceGroup("dev_salon", "Salon", entities=[
            Entity(long_light, "Salon", True, 0, {"supported_color_modes": ["xy"]}),
        ])
        device_set, _ = combined([salon], [script()], previous=previous)
        assert_hub_accepts_mixed(self, device_set)
        merged = next(d for d in device_set["devices"] if d.get("key"))
        self.assertEqual(merged["key"], "scene.salon_legacy_key")

    def test_unavailable_device_does_not_emit_empty_entities_or_collide_name(self):
        # An incomplete hub seed (entities: []) when all entities are offline
        # must not be emitted as an invalid v2 payload; a valid fallback keeps
        # its name reserved against subsequent devices.
        incomplete_prev = {
            "light.offline": {
                "key": "light.offline", "name": "Salon", "entities": [],
                "commands": [{"name": "PowerOn", "value": "e12345678:turn_on"}],
            },
            "light.down": {
                "key": "light.down", "name": "Bureau",
                "entities": [{"alias": "light.down", "entity": "light.down"}],
                "commands": [{"name": "PowerOn", "value": "light.down:turn_on"}],
            },
        }
        groups = [
            devices.DeviceGroup("d1", "Salon", entities=[Entity("light.offline", "Salon", False)]),
            devices.DeviceGroup("d2", "Bureau", entities=[Entity("light.down", "Bureau", False)]),
            devices.DeviceGroup("d3", "Bureau", entities=[Entity("light.bureau_2", "Bureau", True)]),
        ]
        device_set, _ = combined(groups, [], previous=incomplete_prev)
        assert_hub_accepts_mixed(self, device_set)
        keys = {d.get("key") for d in device_set["devices"]}
        self.assertNotIn("light.offline", keys)
        self.assertIn("light.down", keys)
        self.assertIn("light.bureau_2", keys)


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
