"""LG OLED65C4/webOS 24 capture, allowlisting and bounded read lifecycle."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("webos", ROOT / "custom_components/melodious/webos.py")
webos = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webos)
ENTITY = "media_player.tv"
CAPTURE = json.loads((ROOT / "tests/fixtures/lg_external_inputs.json").read_text())
VOLUME = {"volumeStatus": {"soundOutput": "external_arc", "volume": 14,
                          "muteStatus": False, "externalDeviceControl": True}}


class ParsingTests(unittest.TestCase):
    def test_real_sanitised_capture(self):
        ports = webos.parse_inputs(CAPTURE)
        self.assertEqual([p["id"] for p in ports], [f"HDMI_{n}" for n in range(1, 5)])
        self.assertEqual(ports[0], {"id": "HDMI_1", "label": "Living Room TV", "connected": True,
                                  "signal": True, "cec_name": "Living Room TV",
                                  "cec_type": 8, "cec_vendor": 1877008})
        self.assertEqual(ports[1]["cec_type"], 5)
        self.assertFalse(ports[1]["signal"], "ARC does not imply a video signal")
        self.assertFalse(ports[2]["connected"], "hdmiPlugIn/URCU do not imply connected")
        self.assertIsNone(ports[2]["cec_name"], "URCU is not CEC")

    def test_allowlist_drops_private_and_irrelevant_fields(self):
        raw = copy.deepcopy(CAPTURE)
        raw["serialNumber"] = "PRIVATE_SENTINEL"
        raw["devices"][0].update(serialNumber="PRIVATE_SENTINEL", mac="PRIVATE_SENTINEL",
                                 icon="PRIVATE_SENTINEL", physicalAddress="PRIVATE_SENTINEL")
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(webos.parse_inputs(raw)))
        self.assertNotIn("physicalAddress", json.dumps(webos.parse_inputs(raw)))

    def test_malformed_fields_never_invent_state(self):
        ports = webos.parse_inputs({"devices": [None, {}, {"id": "Netflix"},
            {"id": "HDMI_1", "connected": "true", "hdmiSignalExist": 1, "subList": None},
            {"id": "HDMI_1", "label": "duplicate"}]})
        self.assertEqual(len(ports), 1)
        self.assertIsNone(ports[0]["connected"])
        self.assertIsNone(ports[0]["signal"])
        with self.assertRaises(ValueError):
            webos.parse_inputs({})

    def test_sound_is_tv_reported_not_measured_soundbar_power(self):
        sound = webos.parse_sound(VOLUME, webos.parse_inputs(CAPTURE))
        self.assertEqual(sound, {"output": "external_arc", "label": "Nakamichi Sound Bar",
                                "port": "HDMI_2", "volume": 14, "muted": False,
                                "external_control": True})
        self.assertNotIn("power", sound)
        self.assertIsNone(webos.parse_sound({"volumeStatus": {"volume": True}}, [])["volume"])

    def test_foreground_is_identity_not_a_cec_label_guess(self):
        ports = webos.parse_inputs(CAPTURE)
        self.assertEqual(webos.active_input({"appId": "com.webos.app.hdmi1"}, ports),
                         {"id": "HDMI_1", "label": "Living Room TV"})
        self.assertIsNone(webos.active_input({"appId": "netflix"}, ports))
        self.assertIsNone(webos.active_input({"appId": "com.webos.app.hdmi9"}, ports))
        with self.assertRaises(ValueError):
            webos.active_input({}, ports)

    def test_ha_entity_response_and_rest_wrapper(self):
        body = {"returnValue": True, **CAPTURE}
        for response in ({ENTITY: body}, {"service_response": {ENTITY: body}}):
            self.assertEqual(webos.response_body(response, ENTITY), body)
        for response in (None, {}, {ENTITY: {"returnValue": False}}):
            with self.assertRaises(ValueError):
                webos.response_body(response, ENTITY)


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.on = True
        self.now = 100
        self.calls = []
        self.fail = set()
        self.responses = {webos.INPUTS: CAPTURE, webos.VOLUME: VOLUME,
                          webos.FOREGROUND: {"appId": "com.webos.app.hdmi1"}}
        self.observations = webos.WebOSObservations(self.fetch, lambda eid: self.on, lambda: self.now)

    async def fetch(self, eid, uri):
        self.calls.append((eid, uri))
        if uri in self.fail:
            raise RuntimeError("private error must never be logged/stored")
        return {eid: {"returnValue": True, **self.responses[uri]}}

    async def test_off_sends_no_reads_or_control_commands(self):
        self.on = False
        await self.observations.refresh([ENTITY])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.observations.theater(ENTITY))

    async def test_success_and_expiry_keep_metadata_not_live_state(self):
        await self.observations.refresh([ENTITY, ENTITY])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.observations.theater(ENTITY)["sound"]["volume"], 14)
        self.now += webos.FRESH_SECONDS
        self.assertIsNone(self.observations.theater(ENTITY))
        self.assertEqual(len(self.observations.ports[ENTITY]), 4)

    async def test_partial_failure_clears_only_failed_fields(self):
        await self.observations.refresh([ENTITY])
        self.fail.add(webos.VOLUME)
        await self.observations.refresh([ENTITY])
        live = self.observations.theater(ENTITY)
        self.assertIsNone(live["sound"])
        self.assertEqual(live["active_input"]["id"], "HDMI_1")
        self.fail.update((webos.INPUTS, webos.FOREGROUND))
        await self.observations.refresh([ENTITY])
        self.assertEqual(self.observations.theater(ENTITY), {"ports": None, "sound": None,
                                                          "active_input": None, "foreground_app": None})
        self.assertEqual(len(self.observations.ports[ENTITY]), 4)

    async def test_off_during_read_cannot_restore_state(self):
        async def fetch(eid, uri):
            result = await self.fetch(eid, uri)
            self.on = False
            return result
        self.observations.fetch = fetch
        await self.observations.refresh([ENTITY])
        self.assertIsNone(self.observations.theater(ENTITY))
        self.assertEqual(self.observations.live, {})

    async def test_overlapping_polls_do_not_queue(self):
        gate = asyncio.Event()
        async def fetch(eid, uri):
            await gate.wait()
            return await self.fetch(eid, uri)
        self.observations.fetch = fetch
        first = asyncio.create_task(self.observations.refresh([ENTITY]))
        await asyncio.sleep(0)
        await self.observations.refresh([ENTITY])
        gate.set()
        await first
        self.assertEqual(len(self.calls), 3)
