"""Home Assistant entities as Harmony devices: pure rules, no Home Assistant imports.

The hub's offline writer (codexactivity CommitHaDevices, POST /api/ha-devices)
turns each device here into a Harmony device whose commands are IP commands.
When one runs, the hub posts {"device": <entity_id>, "command": <value>} to
this integration's webhook, and service_call() turns the value back into a
service call. A value is "<action>" or "<action>:<argument>".

The limits below match the hub's writer; it refuses a set that breaks them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_DOMAINS = (
    "media_player",
    "light",
    "switch",
    "input_boolean",
    "fan",
    "cover",
    "script",
    "scene",
    "button",
    "input_button",
)

MAX_DEVICES = 64
MAX_COMMANDS = 128
MAX_INPUTS = 32
MAX_NAME = 64
MAX_VALUE = 96

# Feature bits, as Home Assistant defines them.
MP_PAUSE = 1
MP_VOLUME_SET = 4
MP_VOLUME_MUTE = 8
MP_PREVIOUS_TRACK = 16
MP_NEXT_TRACK = 32
MP_TURN_ON = 128
MP_TURN_OFF = 256
MP_VOLUME_STEP = 1024
MP_SELECT_SOURCE = 2048
MP_STOP = 4096
MP_PLAY = 16384
FAN_SET_SPEED = 1
COVER_OPEN = 1
COVER_CLOSE = 2
COVER_STOP = 8

_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
# The hub fills placeholders with string.gsub and puts values in a JSON body.
_BAD_VALUE_RE = re.compile(r'[\x00-\x1f\x7f"\\%{}]')
_BAD_LABEL_RE = re.compile(r'[\x00-\x1f\x7f"\\]')

# Which actions a webhook call may run, per domain: action -> (service, data).
_ACTIONS: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {
    "media_player": {
        "turn_on": ("turn_on", {}),
        "turn_off": ("turn_off", {}),
        "volume_up": ("volume_up", {}),
        "volume_down": ("volume_down", {}),
        "media_play": ("media_play", {}),
        "media_pause": ("media_pause", {}),
        "media_play_pause": ("media_play_pause", {}),
        "media_stop": ("media_stop", {}),
        "media_next_track": ("media_next_track", {}),
        "media_previous_track": ("media_previous_track", {}),
    },
    "light": {
        "turn_on": ("turn_on", {}),
        "turn_off": ("turn_off", {}),
        "toggle": ("toggle", {}),
        "brightness_up": ("turn_on", {"brightness_step_pct": 10}),
        "brightness_down": ("turn_on", {"brightness_step_pct": -10}),
    },
    "switch": {"turn_on": ("turn_on", {}), "turn_off": ("turn_off", {}), "toggle": ("toggle", {})},
    "input_boolean": {"turn_on": ("turn_on", {}), "turn_off": ("turn_off", {}), "toggle": ("toggle", {})},
    "fan": {
        "turn_on": ("turn_on", {}),
        "turn_off": ("turn_off", {}),
        "toggle": ("toggle", {}),
        "increase_speed": ("increase_speed", {}),
        "decrease_speed": ("decrease_speed", {}),
    },
    "cover": {
        "open_cover": ("open_cover", {}),
        "close_cover": ("close_cover", {}),
        "stop_cover": ("stop_cover", {}),
    },
    "script": {"turn_on": ("turn_on", {})},
    "scene": {"turn_on": ("turn_on", {})},
    "button": {"press": ("press", {})},
    "input_button": {"press": ("press", {})},
}


@dataclass
class Entity:
    """What the device set needs from one entity's state."""

    entity_id: str
    name: str
    available: bool = True
    supported_features: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    # HA device registry data, when the entity was picked via a device.
    device_id: str | None = None
    platform: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass
class DeviceGroup:
    """One HA device (registry entry) and its candidate entities."""

    device_id: str
    name: str
    manufacturer: str | None = None
    model: str | None = None
    entities: list[Entity] = field(default_factory=list)


def clean_label(text: str, limit: int = MAX_NAME) -> str:
    """A name the hub accepts: no quotes, backslashes or control characters."""
    text = _BAD_LABEL_RE.sub(" ", str(text or ""))
    return " ".join(text.split())[:limit].strip()


def _value_ok(value: str) -> bool:
    return 0 < len(value) <= MAX_VALUE and not _BAD_VALUE_RE.search(value)


def input_command_name(label: str, taken: set[str]) -> str:
    """A source's command name, from its label ("Input HDMI 1"), so a
    reordered source list never re-points a button or an activity."""
    base = ("Input " + " ".join(re.sub(r"[^A-Za-z0-9 _.+-]", "", label).split())).strip()[:44]
    name, suffix = base, 2
    while name.lower() in taken:
        name = f"{base} {suffix}"
        suffix += 1
    return name


def _merge(current: dict, previous: dict | None) -> dict:
    """The current device plus any command or input the previous one had.

    Many players drop their source list (or features) while off, so a device
    rebuilt then would lose commands an activity uses, and every power cycle
    would rewrite the hub's DeviceList and make the Touch sync.
    """
    if not previous:
        return current
    merged = dict(current)
    names = {c["name"].lower() for c in current["commands"]}
    extra = [c for c in previous.get("commands", []) if c["name"].lower() not in names]
    merged["commands"] = (current["commands"] + extra)[:MAX_COMMANDS]
    kept = {c["name"] for c in merged["commands"]}
    inputs = list(current.get("inputs", []))
    seen = {i["name"].lower() for i in inputs}
    for item in previous.get("inputs", []):
        if item["name"].lower() not in seen and item["command"] in kept and len(inputs) < MAX_INPUTS:
            inputs.append(item)
            seen.add(item["name"].lower())
    if inputs:
        merged["inputs"] = inputs
    if "power" not in merged and previous.get("power"):
        merged["power"] = previous["power"]
    return merged


def _commands_for(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """(commands as (name, value), power, inputs) for one entity."""
    domain, features = entity.domain, entity.supported_features
    commands: list[tuple[str, str]] = []
    power = None
    inputs: list[dict] = []

    def add(name: str, value: str, when: bool = True) -> None:
        if when:
            commands.append((name, value))

    if domain == "media_player":
        add("PowerOn", "turn_on", features & MP_TURN_ON)
        add("PowerOff", "turn_off", features & MP_TURN_OFF)
        step = features & (MP_VOLUME_STEP | MP_VOLUME_SET)
        add("VolumeUp", "volume_up", step)
        add("VolumeDown", "volume_down", step)
        add("Mute", "mute", features & MP_VOLUME_MUTE)
        add("Play", "media_play", features & MP_PLAY)
        add("Pause", "media_pause", features & MP_PAUSE)
        add("PlayPause", "media_play_pause", features & (MP_PLAY | MP_PAUSE))
        add("Stop", "media_stop", features & MP_STOP)
        add("SkipForward", "media_next_track", features & MP_NEXT_TRACK)
        add("SkipBack", "media_previous_track", features & MP_PREVIOUS_TRACK)
        if features & MP_SELECT_SOURCE:
            seen: set[str] = set()
            for source in entity.attributes.get("source_list") or []:
                label = clean_label(source)
                value = f"select_source:{source}"
                # A source the hub can't carry exactly would select the wrong one.
                if label != source or not _value_ok(value) or label.lower() in seen:
                    continue
                if len(inputs) == MAX_INPUTS:
                    break
                seen.add(label.lower())
                name = input_command_name(label, {n.lower() for n, _ in commands})
                add(name, value)
                inputs.append({"name": label, "command": name})
    elif domain in ("light", "switch", "input_boolean", "fan"):
        add("PowerOn", "turn_on")
        add("PowerOff", "turn_off")
        add("PowerToggle", "toggle")
        modes = entity.attributes.get("supported_color_modes") or []
        add("BrightnessUp", "brightness_up", domain == "light" and any(m != "onoff" for m in modes))
        add("BrightnessDown", "brightness_down", domain == "light" and any(m != "onoff" for m in modes))
        add("SpeedUp", "increase_speed", domain == "fan" and features & FAN_SET_SPEED)
        add("SpeedDown", "decrease_speed", domain == "fan" and features & FAN_SET_SPEED)
    elif domain == "cover":
        add("Open", "open_cover", features & COVER_OPEN)
        add("Close", "close_cover", features & COVER_CLOSE)
        add("Stop", "stop_cover", features & COVER_STOP)
    elif domain in ("script", "scene"):
        add("Run" if domain == "script" else "Activate", "turn_on")
    elif domain in ("button", "input_button"):
        add("Press", "press")

    names = {name for name, _ in commands}
    if {"PowerOn", "PowerOff"} <= names:
        power = {"on": "PowerOn", "off": "PowerOff"}
    return commands[:MAX_COMMANDS], power, inputs


def device_spec(entity: Entity, name: str) -> dict | None:
    """The hub's device for one entity, or None when it would have no commands."""
    if entity.domain not in SUPPORTED_DOMAINS or not _ENTITY_RE.match(entity.entity_id):
        return None
    commands, power, inputs = _commands_for(entity)
    if not commands:
        return None
    spec: dict[str, Any] = {
        "entity": entity.entity_id,
        "name": name,
        "commands": [{"name": n, "value": v} for n, v in commands],
    }
    if power:
        spec["power"] = power
    if inputs:
        spec["inputs"] = inputs
    return spec


def build_device_set(
    webhook: str,
    entities: list[Entity],
    previous: dict[str, dict] | None = None,
    taken_names: set[str] | None = None,
) -> dict:
    """The set for POST /api/ha-devices.

    Each device keeps every command and input it had in `previous` (by
    entity), and an unavailable entity keeps its device as it was, so an
    outage or a device switched off never drops something an activity uses.
    Names stay unique, and a name another hub device has gets " (HA)".
    """
    previous = previous or {}
    taken = {n.lower() for n in taken_names or ()}
    used: set[str] = set()
    devices = []
    for entity in sorted(entities, key=lambda e: e.entity_id)[:MAX_DEVICES]:
        base = clean_label(entity.name) or entity.entity_id
        name = base
        if name.lower() in taken:
            name = clean_label(f"{base} (HA)")
        suffix = 2
        while name.lower() in used or name.lower() in taken:
            name = clean_label(f"{base[: MAX_NAME - 8]} {suffix}")
            suffix += 1
        before = previous.get(entity.entity_id)
        spec = device_spec(entity, name) if entity.available else None
        if spec:
            spec = _merge(spec, before)
        elif before:
            spec = dict(before, name=name)
        if spec:
            used.add(name.lower())
            devices.append(spec)
    return {"webhook": webhook, "devices": devices}


def allowed_values(device_set: dict) -> dict[str, set[str]]:
    """Each device key's command values, the only ones a webhook call may run.

    The key is the v1 entity ID or the v2 device key; the values are the full
    command values (bare actions for v1, `<alias>:<action>` for v2).
    """
    out = {}
    for d in device_set.get("devices", []):
        out[d.get("key") or d.get("entity")] = {c["value"] for c in d["commands"]}
    return out


def _short_alias(entity_id: str) -> str:
    """A stable short alias when the full entity ID would overflow 96 chars."""
    import hashlib

    return "e" + hashlib.sha1(entity_id.encode()).hexdigest()[:8]


def merge_device(
    group: DeviceGroup,
    key: str,
    previous: dict | None = None,
    disabled_commands: set[str] | None = None,
) -> dict | None:
    """One v2 device spec from a device group's entities.

    Commands come from each available entity's domain profile, prefixed with
    the entity alias; the first command wins each canonical name
    (case-insensitive). `previous` is the last sent spec for this key, whose
    commands/inputs survive by name. Returns None when nothing mappable
    remains.
    """
    disabled = {n.lower() for n in disabled_commands or ()}
    commands: list[tuple[str, str]] = []
    seen: set[str] = set()
    power = None
    inputs: list[dict] = []
    entities_table: list[dict] = []
    for entity in sorted(group.entities, key=lambda e: e.entity_id):
        if not _ENTITY_RE.match(entity.entity_id):
            continue
        alias = entity.entity_id
        entities_table.append({"alias": alias, "entity": entity.entity_id})
        if entity.domain not in SUPPORTED_DOMAINS:
            continue
        names, entity_power, entity_inputs = _commands_for(entity)
        if not names:
            continue
        probe = f"{alias}:{names[0][1]}"
        if len(probe) > MAX_VALUE:
            alias = _short_alias(entity.entity_id)
            entities_table[-1]["alias"] = alias
        if not entity.available:
            continue
        for name, action in names:
            if name.lower() in seen or name.lower() in disabled:
                continue
            seen.add(name.lower())
            commands.append((name, f"{alias}:{action}"))
        if entity_power and power is None:
            on = next((n for n, _ in commands if n == entity_power.get("on")), None)
            off = next((n for n, _ in commands if n == entity_power.get("off")), None)
            toggle = next((n for n, _ in commands if n == entity_power.get("toggle")), None)
            if toggle:
                power = {"toggle": toggle}
            elif on and off:
                power = {"on": on, "off": off}
        for item in entity_inputs:
            if len(inputs) >= MAX_INPUTS:
                break
            if item["name"].lower() in {i["name"].lower() for i in inputs}:
                continue
            renamed = dict(item, command=next(
                (n for n, _ in commands if n == item["command"]), item["command"]))
            inputs.append(renamed)
    if not commands:
        return None
    commands = commands[:MAX_COMMANDS]
    spec: dict[str, Any] = {
        "key": key,
        "name": group.name,
        "entities": entities_table,
        "commands": [{"name": n, "value": v} for n, v in commands],
    }
    if group.model:
        spec["model"] = group.model
    if power:
        spec["power"] = power
    if inputs:
        spec["inputs"] = inputs
    if previous:
        spec = _merge(spec, previous)
        if disabled:
            # The previous-set merge keeps old commands by name; a disabled
            # command must not come back through it.
            spec["commands"] = [c for c in spec["commands"]
                                if c["name"].lower() not in disabled]
            kept = {c["name"] for c in spec["commands"]}
            spec["inputs"] = [i for i in spec.get("inputs", []) if i["command"] in kept]
    return spec


def suppress_cast_twins(groups: list[DeviceGroup]) -> tuple[list[DeviceGroup], list[str]]:
    """Drop Cast-only devices that duplicate a real webOS/Android TV device.

    A Cast twin shares the TV's name with a device that has a webostv,
    androidtv_remote or androidtv entity. Returns (kept, suppressed names).
    """
    real_names = set()
    for group in groups:
        platforms = {e.platform for e in group.entities}
        if platforms & {"webostv", "androidtv_remote", "androidtv"}:
            real_names.add(group.name.strip().lower())
    kept, suppressed = [], []
    for group in groups:
        platforms = {e.platform for e in group.entities}
        domains = {e.domain for e in group.entities}
        if platforms == {"cast"} and domains == {"media_player"} \
                and group.name.strip().lower() in real_names:
            suppressed.append(group.name)
            continue
        kept.append(group)
    return kept, suppressed


def build_merged_set(
    webhook: str,
    groups: list[DeviceGroup],
    keys: dict[str, str],
    disabled: dict[str, set[str]],
    previous: dict[str, dict],
    taken_names: set[str] | None = None,
) -> tuple[dict, list[str]]:
    """The v2 set for POST /api/ha-devices from device groups.

    `keys` maps HA device_id to the stable hub key; `disabled` maps it to
    disabled command names; `previous` is the last sent set indexed by key
    and by entity. Returns (set, suppressed Cast-twin names).
    """
    taken = {n.lower() for n in taken_names or ()}
    used: set[str] = set()
    groups, suppressed = suppress_cast_twins(groups)
    devices = []
    for group in sorted(groups, key=lambda g: g.name.lower())[:MAX_DEVICES]:
        base = clean_label(group.name) or keys.get(group.device_id, group.device_id)
        name = base
        if name.lower() in taken:
            name = clean_label(f"{base} (HA)")
        suffix = 2
        while name.lower() in used or name.lower() in taken:
            name = clean_label(f"{base[: MAX_NAME - 8]} {suffix}")
            suffix += 1
        key = keys.get(group.device_id)
        if not key:
            before = None
            for entity in sorted(group.entities, key=lambda e: e.entity_id):
                if entity.entity_id in previous:
                    before = previous[entity.entity_id]
                    break
            key = before.get("key", before.get("entity")) if before else group.entities[0].entity_id \
                if group.entities else group.device_id
        before = previous.get(key)
        if before is None:
            for entity in group.entities:
                if entity.entity_id in previous:
                    before = previous[entity.entity_id]
                    break
        named = DeviceGroup(group.device_id, name, group.manufacturer, group.model, group.entities)
        spec = merge_device(named, key, before, disabled.get(group.device_id))
        if spec:
            used.add(name.lower())
            devices.append(spec)
        elif before:
            devices.append(dict(before, name=name))
    return {"webhook": webhook, "devices": devices}, suppressed


def resolve_v2_command(
    device_spec: dict, command_value: str
) -> tuple[str, str] | None:
    """(entity_id, action) for a v2 command value, or None when unknown."""
    alias, sep, action = str(command_value).partition(":")
    if not sep or not action:
        return None
    table = {item["alias"]: item["entity"] for item in device_spec.get("entities", [])}
    entity_id = table.get(alias)
    if entity_id is None:
        return None
    return entity_id, action


def service_call(
    entity_id: str, value: str, attributes: dict[str, Any] | None = None,
    platform: str | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """(domain, service, data) for one webhook call, or None if it isn't one we run."""
    domain = entity_id.split(".", 1)[0]
    action, _, argument = str(value).partition(":")
    target = {"entity_id": entity_id}
    if platform == "webostv" and action == "button" and argument:
        return "webostv", "button", {**target, "button": argument}
    if domain == "media_player" and action == "select_source" and argument:
        return domain, "select_source", {**target, "source": argument}
    if domain == "media_player" and action == "mute" and not argument:
        muted = bool((attributes or {}).get("is_volume_muted"))
        return domain, "volume_mute", {**target, "is_volume_muted": not muted}
    known = _ACTIONS.get(domain, {}).get(action)
    if not known or argument:
        return None
    service, data = known
    return domain, service, {**target, **data}
