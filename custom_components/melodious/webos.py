"""Allowlisted LG theater observations through HA's existing webOS connection.

No raw responses, icons, unique IDs, physical addresses, serials or MACs are
retained. Connection and video signal are observations, NOT device power.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

INPUTS = "tv/getExternalInputList"
VOLUME = "audio/getVolume"
FOREGROUND = "com.webos.applicationManager/getForegroundAppInfo"
POLL_SECONDS = 5
FRESH_SECONDS = 8


def response_body(response: Any, entity_id: str) -> dict:
    """HA entity-service responses are keyed by entity (REST adds a wrapper)."""
    if not isinstance(response, dict):
        raise ValueError("invalid webOS response")
    response = response.get("service_response", response)
    body = response.get(entity_id) if isinstance(response, dict) else None
    if not isinstance(body, dict) or body.get("returnValue") is not True:
        raise ValueError("unsuccessful webOS response")
    return body


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())[:64] or None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def parse_inputs(body: dict) -> list[dict]:
    """Only actual HDMI ports; URCU configuration is not CEC or live state."""
    devices = body.get("devices")
    if not isinstance(devices, list):
        raise ValueError("missing webOS ports")
    ports = {}
    for device in devices:
        if not isinstance(device, dict):
            continue
        port_id = device.get("id")
        if not isinstance(port_id, str) or not re.fullmatch(r"HDMI_[1-9]\d?", port_id):
            continue
        if port_id in ports:
            continue
        children = device.get("subList")
        cec = next((c for c in children if isinstance(c, dict) and c.get("id") == "SIMPLINK"), {}) \
            if isinstance(children, list) else {}
        ports[port_id] = {
            "id": port_id,
            "label": _text(device.get("label")) or port_id.replace("_", " "),
            "connected": _bool(device.get("connected")),
            "signal": _bool(device.get("hdmiSignalExist")),
            "cec_name": _text(cec.get("osdName")),
            "cec_type": _integer(cec.get("cecpDevType")),
            "cec_vendor": _integer(cec.get("vendorId")),
        }
    return sorted(ports.values(), key=lambda p: int(p["id"].split("_")[1]))


def parse_sound(body: dict, ports: list[dict]) -> dict:
    status = body.get("volumeStatus")
    if not isinstance(status, dict):
        raise ValueError("missing webOS volume status")
    output = _text(status.get("soundOutput"))
    # An audio-system CEC device identifies the ARC destination, not its power.
    audio = [p for p in ports if p.get("cec_type") == 5 and p.get("connected") is True]
    destination = audio[0] if output == "external_arc" and len(audio) == 1 else None
    volume = status.get("volume")
    if type(volume) not in (int, float) or not 0 <= volume <= 100:
        volume = None
    return {
        "output": output,
        "label": destination["label"] if destination else None,
        "port": destination["id"] if destination else None,
        "volume": volume,
        "muted": _bool(status.get("muteStatus")),
        "external_control": _bool(status.get("externalDeviceControl")),
    }


def active_input(body: dict, ports: list[dict]) -> dict | None:
    app_id = body.get("appId")
    if not isinstance(app_id, str) or not app_id:
        raise ValueError("missing foreground app")
    match = re.fullmatch(r"com\.webos\.app\.hdmi([1-9]\d?)", app_id)
    port = next((p for p in ports if p["id"] == f"HDMI_{match[1]}"), None) if match else None
    return {"id": port["id"], "label": port["label"]} if port else None


class WebOSObservations:
    """Independent, bounded reads; cached port metadata is separate from state.

    fetch(entity_id, URI) uses HA services; is_on() is checked again after
    each await so an off transition cannot resurrect stale live observations.
    """

    def __init__(self, fetch, is_on, clock=time.monotonic) -> None:
        self.fetch = fetch
        self.is_on = is_on
        self.clock = clock
        self.ports: dict[str, list[dict]] = {}
        self.live: dict[str, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    def invalidate(self, entity_id: str) -> None:
        self.live.pop(entity_id, None)

    def theater(self, entity_id: str) -> dict | None:
        live = self.live.get(entity_id)
        if not self.is_on(entity_id) or not live or self.clock() - live[0] >= FRESH_SECONDS:
            return None
        return live[1]

    async def refresh(self, entity_ids: list[str]) -> None:
        # Skip overlapping interval/event reads rather than build a queue.
        if self._lock.locked():
            return
        async with self._lock:
            await asyncio.gather(*(self._refresh_one(eid) for eid in set(entity_ids)))

    async def _refresh_one(self, entity_id: str) -> None:
        if not self.is_on(entity_id):
            self.invalidate(entity_id)
            return
        async def read(uri):
            async with asyncio.timeout(4):
                return response_body(await self.fetch(entity_id, uri), entity_id)
        # Each field fails independently; a failed volume read must not conceal
        # current HDMI signal. Never keep last-successful state on read errors.
        results = await asyncio.gather(*(read(uri) for uri in (INPUTS, VOLUME, FOREGROUND)),
                                       return_exceptions=True)
        if not self.is_on(entity_id):
            self.invalidate(entity_id)
            return
        ports = None
        if isinstance(results[0], dict):
            try:
                ports = parse_inputs(results[0])
                self.ports[entity_id] = ports
            except ValueError:
                pass
        sound, active, foreground = None, None, None
        if isinstance(results[1], dict):
            try:
                sound = parse_sound(results[1], ports or [])
            except ValueError:
                pass
        if isinstance(results[2], dict):
            try:
                active = active_input(results[2], ports or [])
                foreground = _text(results[2].get("appId"))
            except ValueError:
                pass
        self.live[entity_id] = (self.clock(), {"ports": ports, "sound": sound,
                                             "active_input": active, "foreground_app": foreground})
