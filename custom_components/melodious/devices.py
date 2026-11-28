"""Home Assistant entities as Harmony devices: pure rules, no Home Assistant imports.

The hub's offline writer (codexactivity CommitHaDevices, POST /api/ha-devices)
turns each device here into a Harmony device whose commands are IP commands.
When one runs, the hub posts {"device": <key>, "command": <value>} to this
integration's webhook, and service_call() turns the value back into a service
call. A v1 value is "<action>" or "<action>:<argument>"; a v2 value is
"<entity-alias>:<action>[:<argument>]".

Profiles are keyed on (domain, platform), where platform is the entity
registry's integration name. The v1 path keeps the old domain-only behaviour
so an existing entity device never changes on upgrade; the v2 merge path uses
the platform profiles.

The limits below match the hub's writer; it refuses a set that breaks them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_DOMAINS = (
    "media_player",
    "remote",
    "select",
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
MAX_ENTITIES = 32
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
REMOTE_ACTIVITY = 4
FAN_SET_SPEED = 1
COVER_OPEN = 1
COVER_CLOSE = 2
COVER_STOP = 8

_ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
# The hub fills placeholders with string.gsub and puts values in a JSON body.
_BAD_VALUE_RE = re.compile(r'[\x00-\x1f\x7f"\\%{}]')
_BAD_LABEL_RE = re.compile(r'[\x00-\x1f\x7f"\\]')

# Source names that are inputs, not apps. Matched as whole words so an app
# called "Apple TV" or "LG Travel+" is not mistaken for one.
_INPUT_WORDS = ("hdmi", "av", "a/v", "component", "composite", "live tv",
                "antenna", "usb", "scart", "vga", "dvi", "tuner", "aux")

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
    state: str | None = None
    # Allowlisted webOS observations, never raw service responses.
    webos_inputs: list[dict[str, Any]] | None = None
    webos_theater: dict[str, Any] | None = None

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


_TRANSLITERATE_MAP = str.maketrans({
    "Æ": "Ae", "æ": "ae",
    "Œ": "Oe", "œ": "oe",
    "ß": "ss", "ẞ": "SS",
    "Ø": "O", "ø": "o",
    "Ł": "L", "ł": "l",
    "Đ": "D", "đ": "d",
    "Ð": "D", "ð": "d",
    "Þ": "Th", "þ": "th",
    "ı": "i",
    "Ĳ": "Ij", "ĳ": "ij",
})


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes UTF-8 bytes without splitting a char."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore").strip()


def _transliterate_ascii(text: str) -> str:
    """Fold accented and Latin-extended letters to ASCII without deleting them."""
    expanded = str(text or "").translate(_TRANSLITERATE_MAP)
    decomposed = unicodedata.normalize("NFKD", expanded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_label(text: str, limit: int = MAX_NAME) -> str:
    """A name the hub accepts: no quotes, backslashes or control characters,
    bounded in UTF-8 bytes to match the hub's Lua length check."""
    text = _BAD_LABEL_RE.sub(" ", str(text or ""))
    return _truncate_utf8(" ".join(text.split()), limit)


def _value_ok(value: str) -> bool:
    return 0 < len(value.encode("utf-8")) <= MAX_VALUE and not _BAD_VALUE_RE.search(value)


def _prefixed_command_name(prefix: str, label: str, taken: set[str]) -> str:
    """A command name from a label ("Input HDMI 1", "App Netflix"), kept
    unique so a reordered list never re-points a button or an activity.

    Command names stay ASCII because codexactivity rejects non-ASCII in
    command names and the Touch/Elite UI slices long command labels by byte
    offset; accented letters are transliterated instead of dropped.
    """
    cleaned = " ".join(re.sub(r"[^A-Za-z0-9 _.+-]", "", _transliterate_ascii(label)).split())
    base = f"{prefix} {cleaned}".strip()[:44].rstrip()
    name, suffix = base, 2
    while name.lower() in taken:
        name = f"{base} {suffix}"
        suffix += 1
    return name


def input_command_name(label: str, taken: set[str]) -> str:
    """A source's command name, from its label ("Input HDMI 1")."""
    return _prefixed_command_name("Input", label, taken)


def _is_input_source(label: str) -> bool:
    """True when a webOS source name looks like a physical input, not an app."""
    text = " ".join(str(label).lower().split())
    if text.startswith("input "):
        return True
    for word in _INPUT_WORDS:
        if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text):
            return True
    return False


def _is_dynamic_action(value: str) -> bool:
    """True when a command value selects a label-derived option, source, or app."""
    action = value.partition(":")[2] if _v2_command_alias(value) else value
    return action.startswith(("select_option:", "select_source:", "turn_on:"))


def _merge(current: dict, previous: dict | None) -> dict:
    """The current device plus any command or input the previous one had.

    Many players drop their source list (or features) while off, so a device
    rebuilt then would lose commands an activity uses, and every power cycle
    would rewrite the hub's DeviceList and make the Touch sync.

    Existing name->value pairs in `previous` are reserved before assigning
    new names so an upgrade (such as transliterating "Détente" alongside
    "Detente") never silently changes which option an existing button runs,
    including under the MAX_COMMANDS cap.
    """
    if not previous:
        return current
    merged = dict(current)
    prev_commands = list(previous.get("commands", []))
    curr_commands = list(current.get("commands", []))
    curr_by_name = {c["name"].lower(): c for c in curr_commands}

    reserved_by_name: dict[str, dict] = {}
    for prev in prev_commands:
        key = prev["name"].lower()
        curr = curr_by_name.get(key)
        if (
            curr is not None
            and curr["value"] != prev["value"]
            and not (_is_dynamic_action(prev["value"]) and _is_dynamic_action(curr["value"]))
        ):
            reserved_by_name[key] = curr
        else:
            reserved_by_name[key] = prev

    # Keep the previous order. Following the current one made a player whose
    # features come and go with its input (denonavr transport keys) reorder
    # an unchanged device, which was re-pushed and made the remote sync.
    reserved: list[dict] = []
    placed: set[str] = set()
    for prev in prev_commands:
        key = prev["name"].lower()
        if key not in placed:
            reserved.append(reserved_by_name[key])
            placed.add(key)

    by_name: dict[str, dict] = {c["name"].lower(): c for c in reserved}
    reserved_by_value: dict[str, list[str]] = {}
    for c in reserved:
        reserved_by_value.setdefault(c["value"], []).append(c["name"])

    added: list[dict] = []
    name_remap: dict[str, str] = {}
    for curr in curr_commands:
        cname, cval = curr["name"], curr["value"]
        key = cname.lower()
        existing = by_name.get(key)
        if existing is not None and existing["value"] == cval:
            name_remap[cname] = existing["name"]
            continue
        base = re.sub(r" \d+$", "", cname)[:44].rstrip()
        if base != cname and cval in reserved_by_value:
            match = next((n for n in reserved_by_value[cval] if n.lower() == base.lower()), None)
            if match is not None:
                name_remap[cname] = match
                continue
        if existing is None:
            added.append(curr)
            by_name[key] = curr
            name_remap[cname] = cname
            continue
        if cval in reserved_by_value:
            reuse = next(
                (n for n in reserved_by_value[cval]
                 if n.lower() == base.lower() or n.lower().startswith(base.lower() + " ")),
                None,
            )
            if reuse is not None:
                name_remap[cname] = reuse
                continue
        suffix = 2
        while True:
            cand = f"{base} {suffix}"
            ckey = cand.lower()
            holder = by_name.get(ckey)
            if holder is None:
                new_cmd = {"name": cand, "value": cval}
                added.append(new_cmd)
                by_name[ckey] = new_cmd
                name_remap[cname] = cand
                break
            if holder["value"] == cval:
                name_remap[cname] = holder["name"]
                break
            suffix += 1

    merged["commands"] = (reserved + added)[:MAX_COMMANDS]
    kept_by_name = {c["name"]: c["value"] for c in merged["commands"]}
    kept_by_value: dict[str, str] = {}
    for c in merged["commands"]:
        kept_by_value.setdefault(c["value"], c["name"])
    curr_value_by_name = {c["name"]: c["value"] for c in curr_commands}

    curr_inputs: dict[str, dict] = {}
    for item in current.get("inputs", []):
        cmd_name = name_remap.get(item["command"], item["command"])
        if cmd_name not in kept_by_name:
            orig_val = curr_value_by_name.get(item["command"])
            cmd_name = kept_by_value.get(orig_val) if orig_val else None
        if cmd_name:
            curr_inputs.setdefault(item["name"].lower(), dict(item, command=cmd_name))

    # Inputs keep the previous order too, taking the current entry where
    # there is one; new inputs go at the end.
    inputs: list[dict] = []
    seen: set[str] = set()
    for item in previous.get("inputs", []):
        key = item["name"].lower()
        if key in seen:
            continue
        chosen = curr_inputs.get(key) or (item if item["command"] in kept_by_name else None)
        if chosen and len(inputs) < MAX_INPUTS:
            inputs.append(chosen)
            seen.add(key)
    for key, item in curr_inputs.items():
        if key not in seen and len(inputs) < MAX_INPUTS:
            inputs.append(item)
            seen.add(key)
    if inputs:
        merged["inputs"] = inputs
    if "power" not in merged and previous.get("power"):
        merged["power"] = previous["power"]
    return merged


def _legacy_commands_for(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """The v1 domain-only profile. Kept byte-for-byte in behaviour so an
    existing entity device does not change when the integration upgrades."""
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
                if len(commands) >= MAX_COMMANDS:
                    break
                seen.add(label.lower())
                name = input_command_name(label, {n.lower() for n, _ in commands})
                add(name, value)
                # Past 32 inputs a source stays a plain command (see above).
                if len(inputs) < MAX_INPUTS:
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
    return commands, power, inputs


def _media_player_base(entity: Entity) -> tuple[list[tuple[str, str]], dict | None]:
    """Power, volume, mute and transport from a media_player's feature bits."""
    features = entity.supported_features
    commands: list[tuple[str, str]] = []

    def add(name: str, value: str, when: bool = True) -> None:
        if when:
            commands.append((name, value))

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

    power = None
    names = {name for name, _ in commands}
    if {"PowerOn", "PowerOff"} <= names:
        power = {"on": "PowerOn", "off": "PowerOff"}
    return commands, power


def _sources_as_apps(entity: Entity, commands: list[tuple[str, str]]) -> None:
    """A TV/player's source list is apps, not inputs (design §3.4)."""
    if not entity.supported_features & MP_SELECT_SOURCE:
        return
    taken = {n.lower() for n, _ in commands}
    seen: set[str] = set()
    apps: list[tuple[str, str]] = []
    for source in entity.attributes.get("source_list") or []:
        source = str(source)
        label = clean_label(source)
        value = f"select_source:{source}"
        if not label or label != source or not _value_ok(value) or label.lower() in seen:
            continue
        seen.add(label.lower())
        name = _prefixed_command_name("App", label, taken)
        taken.add(name.lower())
        apps.append((name, value))
    commands.extend(apps)


def _tv_media_player(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """A player that is a source, not a display: no inputs, apps only."""
    commands, power = _media_player_base(entity)
    _sources_as_apps(entity, commands)
    return commands, power, []


def _webostv_media_player(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """LG webOS: media_player services plus webostv.button navigation (§3.2)."""
    features = entity.supported_features
    commands: list[tuple[str, str]] = []
    inputs: list[dict] = []

    def add(name: str, value: str, when: bool = True) -> None:
        if when:
            commands.append((name, value))

    has_on = bool(features & MP_TURN_ON)
    has_off = bool(features & MP_TURN_OFF)
    add("PowerOn", "turn_on", has_on)
    add("PowerOff", "turn_off", has_off)
    # Only one discrete direction (or neither) means the honest power path is
    # the TV's own POWER button, not a turn_on the integration cannot promise.
    add("PowerToggle", "button:POWER", not (has_on and has_off))
    power = None
    if has_on and has_off:
        power = {"on": "PowerOn", "off": "PowerOff"}
    elif "PowerToggle" in {n for n, _ in commands}:
        power = {"toggle": "PowerToggle"}

    if features & MP_VOLUME_STEP:
        add("VolumeUp", "volume_up")
        add("VolumeDown", "volume_down")
    elif features & MP_VOLUME_SET:
        # No step service: the TV's own volume buttons are still step-based.
        add("VolumeUp", "button:VOLUMEUP")
        add("VolumeDown", "button:VOLUMEDOWN")
    add("Mute", "mute", features & MP_VOLUME_MUTE)
    add("Play", "media_play", features & MP_PLAY)
    add("Pause", "media_pause", features & MP_PAUSE)
    add("PlayPause", "media_play_pause", features & (MP_PLAY | MP_PAUSE))
    add("Stop", "media_stop", features & MP_STOP)
    # Back before any Previous* command: the Touch binds PrevChannel by alias.
    add("Back", "button:BACK")
    add("SkipForward", "media_next_track", features & MP_NEXT_TRACK)
    add("SkipBack", "media_previous_track", features & MP_PREVIOUS_TRACK)

    add("DirectionUp", "button:UP")
    add("DirectionDown", "button:DOWN")
    add("DirectionLeft", "button:LEFT")
    add("DirectionRight", "button:RIGHT")
    add("Select", "button:ENTER")
    # Menu before Home: the Touch's Menu key accepts the "home" alias.
    add("Menu", "button:MENU")
    add("Home", "button:HOME")
    add("Guide", "button:GUIDE")
    add("Info", "button:INFO")
    add("Exit", "button:EXIT")
    for digit in "0123456789":
        add(digit, f"button:{digit}")
    add("Dash", "button:DASH")
    add("Record", "button:RECORD")
    add("Red", "button:RED")
    add("Green", "button:GREEN")
    add("Yellow", "button:YELLOW")
    add("Blue", "button:BLUE")
    add("ChannelUp", "button:CHANNELUP")
    add("ChannelDown", "button:CHANNELDOWN")

    if features & MP_SELECT_SOURCE and entity.webos_inputs is not None:
        taken = {n.lower() for n, _ in commands}
        seen: set[str] = set()
        apps: list[tuple[str, str]] = []
        port_labels = set()
        for port in entity.webos_inputs:
            label = port["id"].replace("_", " ")
            name = f"Input {label}"
            add(name, f"webos_input:{port['id']}")
            taken.add(name.lower())
            inputs.append({"name": label, "command": name})
            port_labels.add(port["label"].casefold())
            if port.get("cec_name"):
                port_labels.add(port["cec_name"].casefold())
        for source in entity.attributes.get("source_list") or []:
            source = str(source)
            label = clean_label(source)
            value = f"select_source:{source}"
            if label.casefold() in port_labels or re.fullmatch(r"HDMI[ _]?\d+", label, re.I):
                continue
            if not label or label != source or not _value_ok(value) or label.lower() in seen:
                continue
            seen.add(label.lower())
            name = _prefixed_command_name("App", label, taken)
            taken.add(name.lower())
            apps.append((name, value))
        # Apps last: the 128-command trim drops them before real controls.
        commands.extend(apps)

    return commands, power, inputs


def _androidtv_remote_remote(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """Google TV navigation via the androidtv_remote remote entity (§3.3)."""
    commands: list[tuple[str, str]] = []

    def add(name: str, key: str) -> None:
        commands.append((name, f"send_command:{key}"))

    add("DirectionUp", "DPAD_UP")
    add("DirectionDown", "DPAD_DOWN")
    add("DirectionLeft", "DPAD_LEFT")
    add("DirectionRight", "DPAD_RIGHT")
    add("Select", "DPAD_CENTER")
    add("Back", "BACK")
    # Menu before Home, as above.
    add("Menu", "MENU")
    add("Home", "HOME")
    for digit in "0123456789":
        add(digit, digit)
    add("Dash", "MINUS")
    add("Red", "PROG_RED")
    add("Green", "PROG_GREEN")
    add("Yellow", "PROG_YELLOW")
    add("Blue", "PROG_BLUE")
    # No RECORD key in androidtvremote2 0.2.3 (HA 2026.1.2); MEDIA_RECORD is
    # a different key, so Record is deliberately not emitted.

    taken = {n.lower() for n, _ in commands}
    apps: list[tuple[str, str]] = []
    for app in entity.attributes.get("activity_list") or []:
        app = str(app)
        label = clean_label(app)
        value = f"turn_on:{app}"
        if not label or label != app or not _value_ok(value):
            continue
        name = _prefixed_command_name("App", label, taken)
        taken.add(name.lower())
        apps.append((name, value))
    commands.extend(apps)
    return commands, None, []


def _androidtv_adb_remote(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """Older Android TV / Fire TV (ADB) navigation (§3.3).

    Key names are androidtv 0.0.75's constants.KEYS (HA 2026.1.2). That table
    has no digits, Dash, Record, Guide, Info, Exit or channel keys, so those
    are deliberately not emitted. Code-and-unit-test only: the owner has no
    ADB device.
    """
    commands: list[tuple[str, str]] = []

    def add(name: str, key: str) -> None:
        commands.append((name, f"send_command:{key}"))

    add("DirectionUp", "UP")
    add("DirectionDown", "DOWN")
    add("DirectionLeft", "LEFT")
    add("DirectionRight", "RIGHT")
    add("Select", "ENTER")
    add("Back", "BACK")
    add("Menu", "MENU")
    add("Home", "HOME")
    add("Red", "RED")
    add("Green", "GREEN")
    add("Yellow", "YELLOW")
    add("Blue", "BLUE")
    return commands, None, []


def _select_commands(entity: Entity) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """An AVR source selector: each option becomes an Input command (§3.5)."""
    commands: list[tuple[str, str]] = []
    inputs: list[dict] = []
    taken: set[str] = set()
    seen: set[str] = set()
    for option in entity.attributes.get("options") or []:
        option = str(option)
        label = clean_label(option)
        value = f"select_option:{option}"
        if not label or label != option or not _value_ok(value) or label.lower() in seen:
            continue
        if len(commands) >= MAX_COMMANDS:
            break
        seen.add(label.lower())
        name = input_command_name(label, taken)
        taken.add(name.lower())
        commands.append((name, value))
        # The hub allows 32 inputs but 128 commands: options past the 32nd
        # stay usable as plain commands (activities, buttons, send_command).
        if len(inputs) < MAX_INPUTS:
            inputs.append({"name": label, "command": name})
    return commands, None, inputs


def _commands_for(
    entity: Entity, merged: bool = False
) -> tuple[list[tuple[str, str]], dict | None, list[dict]]:
    """(commands as (name, value), power, inputs) for one entity.

    `merged` selects the v2 platform profiles. The v1 path stays domain-only
    for media players so an existing entity device is unchanged.
    """
    domain, platform = entity.domain, entity.platform
    if domain == "media_player":
        if merged and platform == "webostv":
            return _webostv_media_player(entity)
        if merged and platform in ("androidtv_remote", "androidtv", "cast"):
            return _tv_media_player(entity)
        return _legacy_commands_for(entity)
    if domain == "remote":
        if platform == "androidtv_remote":
            return _androidtv_remote_remote(entity)
        if platform == "androidtv":
            return _androidtv_adb_remote(entity)
        # An unknown remote's key names are not known; do not guess.
        return [], None, []
    if domain == "select":
        return _select_commands(entity)
    return _legacy_commands_for(entity)


def device_spec(entity: Entity, name: str) -> dict | None:
    """The hub's device for one entity, or None when it would have no commands."""
    if entity.domain not in SUPPORTED_DOMAINS or not _ENTITY_RE.match(entity.entity_id):
        return None
    commands, power, inputs = _commands_for(entity)
    if not commands:
        return None
    commands = commands[:MAX_COMMANDS]
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
            name = clean_label(f"{clean_label(base, MAX_NAME - 5)} (HA)")
        suffix = 2
        while name.lower() in used or name.lower() in taken:
            name = clean_label(f"{clean_label(base, MAX_NAME - 8)} {suffix}")
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


_V1_PREFIXED_ACTIONS = frozenset({
    "select_source",
    "select_option",
    "send_command",
    "turn_on",
    "button",
    "webos_input",
})
_SHORT_ALIAS_RE = re.compile(r"^e[0-9a-f]{8}$")


def _v2_command_alias(value: str) -> str | None:
    """Return the v2 alias prefix when `value` is `<alias>:<action>`, else None."""
    alias, sep, action = str(value).partition(":")
    if not sep or not action or alias in _V1_PREFIXED_ACTIONS:
        return None
    if _ENTITY_RE.match(alias) or _SHORT_ALIAS_RE.match(alias):
        return alias
    return None


def _v2_previous(previous: dict, entities_table: list[dict]) -> dict | None:
    """A previous spec with v2 aliases, so a v1 device migrates cleanly.

    A v1 previous spec has bare action values; prefixing them with the
    entity's current alias keeps its commands (including old `Input ...`
    names an activity still uses) valid in a v2 device. A v2 previous spec
    only needs its values rewritten if the entity's alias changed.
    """
    alias_by_entity = {item["entity"]: item["alias"] for item in entities_table}
    valid_aliases = set(alias_by_entity.values())
    if previous.get("key") or previous.get("entities"):
        changes = {}
        for item in previous.get("entities", []):
            new = alias_by_entity.get(item["entity"])
            if new and new != item["alias"]:
                changes[item["alias"]] = new
        out = dict(previous)
        out["commands"] = []
        for command in previous.get("commands", []):
            alias, sep, rest = str(command.get("value", "")).partition(":")
            if not sep or not rest:
                continue
            if alias in changes:
                alias = changes[alias]
                command = dict(command, value=f"{alias}:{rest}")
            if alias in valid_aliases:
                out["commands"].append(command)
        return out
    entity = previous.get("entity")
    alias = alias_by_entity.get(entity) if entity else None
    if not alias:
        return None
    out = dict(previous)
    rewritten = []
    for command in previous.get("commands", []):
        val = str(command.get("value", ""))
        existing_alias = _v2_command_alias(val)
        if existing_alias:
            if existing_alias in valid_aliases:
                rewritten.append(command)
            continue
        rewritten.append(dict(command, value=f"{alias}:{val}"))
    out["commands"] = rewritten
    return out


def merge_device(
    group: DeviceGroup,
    key: str,
    previous: dict | None = None,
    disabled_commands: set[str] | None = None,
    warnings: list[str] | None = None,
) -> dict | None:
    """One v2 device spec from a device group's entities.

    Commands come from each available entity's (domain, platform) profile,
    prefixed with the entity alias; the first command wins each canonical
    name (case-insensitive). `previous` is the last sent spec for this key,
    whose commands/inputs survive by name. Returns None when nothing mappable
    remains. When `warnings` is given, a 128-command trim is reported there.
    """
    disabled = {n.lower() for n in disabled_commands or ()}
    commands: list[tuple[str, str]] = []
    seen: set[str] = set()
    power = None
    inputs: list[dict] = []
    entities_table: list[dict] = []
    # An alias never grows back: a TV that's off reports a short source list,
    # but its earlier long app commands still have to fit beside the alias.
    previous_alias = {item.get("entity"): item.get("alias")
                      for item in (previous or {}).get("entities") or []}
    v1_actions = ([str(c.get("value", "")) for c in (previous or {}).get("commands") or []]
                  if previous and previous.get("entity") and not previous.get("entities") else [])
    for entity in sorted(group.entities, key=lambda e: e.entity_id):
        if not _ENTITY_RE.match(entity.entity_id) or entity.domain not in SUPPORTED_DOMAINS:
            continue
        alias = previous_alias.get(entity.entity_id) or entity.entity_id
        if len(alias) > 64:
            alias = _short_alias(entity.entity_id)
        entities_table.append({"alias": alias, "entity": entity.entity_id})
        names, entity_power, entity_inputs = _commands_for(entity, merged=True)
        if not names:
            continue
        actions = [action for _, action in names]
        if previous and previous.get("entity") == entity.entity_id:
            actions += v1_actions
        # The alias must fit the hub's 64-char alias limit and every
        # "<alias>:<action>" value the entity contributes must fit 96 UTF-8 bytes.
        if len(alias) > 64 or any(len(f"{alias}:{action}".encode("utf-8")) > MAX_VALUE for action in actions):
            alias = _short_alias(entity.entity_id)
            entities_table[-1]["alias"] = alias
        if not entity.available:
            continue
        for name, action in names:
            if name.lower() in seen or name.lower() in disabled:
                continue
            value = f"{alias}:{action}"
            if not _value_ok(value):
                continue
            seen.add(name.lower())
            commands.append((name, value))
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
            command = next((n for n, _ in commands if n == item["command"]), None)
            if command is None:
                continue
            inputs.append(dict(item, command=command))
    if not commands:
        return None
    if len(commands) > MAX_COMMANDS:
        dropped = commands[MAX_COMMANDS:]
        dropped_apps = sum(1 for name, _ in dropped if name.startswith("App "))
        if warnings is not None:
            warnings.append(
                f"{group.name}: {len(commands)} commands; kept {MAX_COMMANDS}, "
                f"dropped {len(dropped)} ({dropped_apps} app commands)"
            )
        commands = commands[:MAX_COMMANDS]
    # The hub caps each merged device at MAX_ENTITIES (32) entries in entities.
    # Keep every entity referenced by a command and the device's own key entity
    # first, then fill up to the cap.
    if len(entities_table) > MAX_ENTITIES:
        used_aliases = {value.partition(":")[0] for _, value in commands}
        key_entries = [item for item in entities_table if item["entity"] == key]
        used_entries = [
            item for item in entities_table
            if item["entity"] != key and item["alias"] in used_aliases
        ]
        others = [
            item for item in entities_table
            if item["entity"] != key and item["alias"] not in used_aliases
        ]
        entities_table = (key_entries + used_entries + others)[:MAX_ENTITIES]
        kept_aliases = {item["alias"] for item in entities_table}
        commands = [(n, v) for n, v in commands if v.partition(":")[0] in kept_aliases]
        if not commands:
            return None
    kept = {n for n, _ in commands}
    inputs = [i for i in inputs if i["command"] in kept]
    spec: dict[str, Any] = {
        "key": key,
        "name": group.name,
        "entities": entities_table,
        "commands": [{"name": n, "value": v} for n, v in commands],
    }
    model = clean_label(group.model) if group.model else ""
    if model:
        spec["model"] = model
    if power:
        spec["power"] = power
    if inputs:
        spec["inputs"] = inputs
    previous = _v2_previous(previous, entities_table) if previous else None
    if previous:
        spec = _merge(spec, previous)
    # Upgrade old HDMI actions in place, including misclassified App commands.
    # Keeping their names lets the hub preserve command IDs and references.
    webos = {e.entity_id: e for e in group.entities if e.platform == "webostv"}
    for command in spec["commands"]:
        resolved = resolve_v2_command(spec, command["value"])
        if resolved is None or resolved[0] not in webos:
            continue
        entity = webos[resolved[0]]
        match = re.fullmatch(r"Input HDMI ([1-9]\d*)", command["name"], re.I)
        port_id = f"HDMI_{match[1]}" if match else None
        if resolved[1].startswith("select_source:"):
            label = resolved[1].split(":", 1)[1]
            port_id = port_id or next((p["id"] for p in entity.webos_inputs or []
                                       if label in (p["label"], p.get("cec_name"))), None)
        if port_id:
            alias = command["value"].split(":", 1)[0]
            command["value"] = f"{alias}:webos_input:{port_id}"
    if any(e.webos_inputs is not None for e in webos.values()):
        # Inputs are stable port keys. Legacy app commands remain callable but
        # are no longer offered as physical inputs.
        spec["inputs"] = [i for i in spec.get("inputs", [])
                          if re.fullmatch(r"HDMI [1-9]\d*", i["name"])]
    if disabled:
        # The previous-set merge keeps old commands by name; a disabled
        # command must not come back through it.
        spec["commands"] = [c for c in spec["commands"]
                            if c["name"].lower() not in disabled]
        kept = {c["name"] for c in spec["commands"]}
        inputs = [i for i in spec.get("inputs", []) if i["command"] in kept]
        if inputs:
            spec["inputs"] = inputs
        else:
            spec.pop("inputs", None)
    if spec.get("power"):
        # Recompute the power pair from the surviving commands, so unticking
        # a power command cannot leave the hub a pair that names a missing one.
        names = {c["name"] for c in spec["commands"]}
        power = spec["power"]
        if "toggle" in power:
            if power["toggle"] not in names:
                spec.pop("power")
        elif power.get("on") not in names or power.get("off") not in names:
            spec.pop("power")
    return spec


def _device_name_key(name: str) -> str:
    """A comparable device name: case, punctuation and [brand] tags removed."""
    text = _transliterate_ascii(str(name or "")).lower()
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _same_device_name(first: str, second: str) -> bool:
    """True when two registry names clearly describe the same physical device.

    Exact matches cover the Google TV twins. The LG Cast twin is named
    "[LG] webOS TV OLED65C4PUA" while the real device is "LG webOS TV
    OLED65C4PUA", so a multi-word suffix match covers that without joining
    two differently named TVs in different rooms.
    """
    if not first or not second:
        return False
    if first == second:
        return True
    first_words, second_words = first.split(), second.split()
    if min(len(first_words), len(second_words)) < 2:
        return False
    return first.endswith(" " + second) or second.endswith(" " + first)


def suppress_cast_twins(groups: list[DeviceGroup]) -> tuple[list[DeviceGroup], list[str]]:
    """Drop Cast-only devices that duplicate a real webOS/Android TV device.

    A Cast twin is a separate HA device entry for the same hardware. It is
    suppressed when a device with a webostv, androidtv_remote or androidtv
    entity has the same (normalised) name. Returns (kept, suppressed names).
    """
    real_names = []
    for group in groups:
        platforms = {e.platform for e in group.entities}
        if platforms & {"webostv", "androidtv_remote", "androidtv"}:
            real_names.append(_device_name_key(group.name))
    kept, suppressed = [], []
    for group in groups:
        platforms = {e.platform for e in group.entities}
        domains = {e.domain for e in group.entities}
        name = _device_name_key(group.name)
        if platforms == {"cast"} and domains == {"media_player"} \
                and any(_same_device_name(name, real) for real in real_names):
            suppressed.append(group.name)
            continue
        kept.append(group)
    return kept, suppressed


def _previous_for_group(group: DeviceGroup, previous: dict[str, dict]) -> dict | None:
    """Find a group's earlier spec in `previous` by entity ID or short alias."""
    for entity in sorted(group.entities, key=lambda e: e.entity_id):
        if entity.entity_id in previous:
            return previous[entity.entity_id]
    short_aliases = {
        _short_alias(e.entity_id) for e in group.entities if _ENTITY_RE.match(e.entity_id)
    }
    if short_aliases:
        seen_ids: set[int] = set()
        for spec in previous.values():
            if id(spec) in seen_ids or not spec.get("key"):
                continue
            seen_ids.add(id(spec))
            for command in spec.get("commands") or []:
                if _v2_command_alias(command.get("value", "")) in short_aliases:
                    return spec
    return None


def build_merged_set(
    webhook: str,
    groups: list[DeviceGroup],
    keys: dict[str, str],
    disabled: dict[str, set[str]],
    previous: dict[str, dict],
    taken_names: set[str] | None = None,
    warnings: list[str] | None = None,
) -> tuple[dict, list[str]]:
    """The v2 set for POST /api/ha-devices from device groups.

    `keys` maps HA device_id to the stable hub key; `disabled` maps it to
    disabled command names; `previous` is the last sent set indexed by key
    and by entity. Returns (set, suppressed Cast-twin names). When `warnings`
    is given, per-device trim reasons are appended to it.
    """
    taken = {n.lower() for n in taken_names or ()}
    used: set[str] = set()
    groups, suppressed = suppress_cast_twins(groups)
    devices = []
    for group in sorted(groups, key=lambda g: g.name.lower())[:MAX_DEVICES]:
        base = clean_label(group.name) or keys.get(group.device_id, group.device_id)
        name = base
        if name.lower() in taken:
            name = clean_label(f"{clean_label(base, MAX_NAME - 5)} (HA)")
        suffix = 2
        while name.lower() in used or name.lower() in taken:
            name = clean_label(f"{clean_label(base, MAX_NAME - 8)} {suffix}")
            suffix += 1
        key = keys.get(group.device_id)
        if not key:
            before = _previous_for_group(group, previous)
            supported = [
                e for e in group.entities
                if e.domain in SUPPORTED_DOMAINS and _ENTITY_RE.match(e.entity_id)
            ]
            supported_key = min(supported, key=_entity_priority).entity_id if supported else None
            key = before.get("key", before.get("entity")) if before else (
                supported_key or (group.entities[0].entity_id if group.entities else group.device_id)
            )
        before = previous.get(key) or _previous_for_group(group, previous)
        named = DeviceGroup(group.device_id, name, group.manufacturer, group.model, group.entities)
        spec = merge_device(named, key, before, disabled.get(group.device_id), warnings)
        if spec:
            used.add(name.lower())
            devices.append(spec)
        elif before and (not before.get("key") or before.get("entities")):
            used.add(name.lower())
            devices.append(dict(before, name=name))
    return {"webhook": webhook, "devices": devices}, suppressed


def build_combined_set(
    webhook: str,
    groups: list[DeviceGroup],
    entities: list[Entity],
    keys: dict[str, str],
    disabled: dict[str, set[str]],
    previous: dict[str, dict],
    taken_names: set[str] | None = None,
    warnings: list[str] | None = None,
) -> tuple[dict, list[str]]:
    """Merged v2 devices plus the single entities they don't already cover.

    Picking whole devices must not drop the single entities picked beside
    them (issue #162). A single entity already inside an emitted merged
    device is skipped: the hub refuses a set that lists one entity in two
    devices. Merged devices keep first claim on the 64-device cap and on
    their names, so a colliding single is renamed, never the merged device.
    """
    merged_set, suppressed = build_merged_set(
        webhook, groups, keys, disabled, previous, taken_names, warnings)
    merged_devices = merged_set["devices"]
    # Only devices actually emitted cover an entity: a group that was
    # suppressed (Cast twin) or trimmed to nothing must not hide a single.
    covered: set[str] = set()
    for device in merged_devices:
        if device.get("entity"):
            covered.add(device["entity"])
        covered.update(item["entity"] for item in device.get("entities") or [])
    remaining = [e for e in entities if e.entity_id not in covered]
    room = MAX_DEVICES - len(merged_devices)
    if not remaining or room <= 0:
        return merged_set, suppressed
    # A v2 previous spec would leak alias-prefixed commands (or a whole
    # merged device) into a v1 device; only entity-shaped specs carry over.
    single_previous = {
        key: spec for key, spec in previous.items()
        if spec.get("entity") and not spec.get("entities") and not spec.get("key")
    }
    names = set(taken_names or ()) | {d["name"] for d in merged_devices}
    singles = build_device_set(webhook, remaining, single_previous, names)["devices"]
    return {"webhook": webhook, "devices": merged_devices + singles[:room]}, suppressed


def flatten_deduped(groups: list[DeviceGroup], entities: list[Entity]) -> list[Entity]:
    """Group members then single entities, each entity_id exactly once.

    The pre-v2 hub fallback sends one v1 device per entity; an entity picked
    both as part of a device and on its own must still reach the hub once.
    """
    seen: set[str] = set()
    out: list[Entity] = []
    for entity in [m for g in groups for m in g.entities] + list(entities):
        if entity.entity_id not in seen:
            seen.add(entity.entity_id)
            out.append(entity)
    return out


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
    if domain == "media_player" and platform == "webostv" and action == "webos_input" \
            and re.fullmatch(r"HDMI_[1-9]\d*", argument):
        return "webostv", "command", {
            **target, "command": "tv/switchInput", "payload": {"inputId": argument},
        }
    if domain == "media_player" and action == "select_source" and argument:
        return domain, "select_source", {**target, "source": argument}
    if domain == "media_player" and action == "mute" and not argument:
        muted = bool((attributes or {}).get("is_volume_muted"))
        return domain, "volume_mute", {**target, "is_volume_muted": not muted}
    if domain == "remote" and action == "send_command" and argument:
        return domain, "send_command", {**target, "command": [argument]}
    if domain == "remote" and action == "turn_on" and argument:
        return domain, "turn_on", {**target, "activity": argument}
    if domain == "select" and action == "select_option" and argument:
        return domain, "select_option", {**target, "option": argument}
    known = _ACTIONS.get(domain, {}).get(action)
    if not known or argument:
        return None
    service, data = known
    return domain, service, {**target, **data}


def hub_ha_devices(config: dict) -> list[dict]:
    """The hub's Home Assistant devices from /api/activity-config, as v1 specs.

    In DevicesWithFeatures, Commands is a sibling of Device, not inside it.
    """
    devices = []
    for entry in (config.get("deviceList") or {}).get("DevicesWithFeatures") or []:
        device = entry.get("Device") or {}
        tag = str((device.get("AutoDetectedDevice") or {}).get("Id") or "")
        if not tag.startswith("melodious-ha-"):
            continue
        tokens = (device.get("Tokens") or {}).get("Parameters") or []
        key = next((t.get("Value") for t in tokens if t.get("Key") == "{d}"), None)
        if not key:
            continue
        commands = []
        for command in entry.get("Commands") or []:
            value = next((p.get("Value") for p in command.get("Parameters") or []
                          if p.get("Key") == "{c}"), None)
            if command.get("Name") and value:
                commands.append({"name": command["Name"], "value": value})
        aliases = [_v2_command_alias(c["value"]) for c in commands]
        if commands and all(aliases):
            entity_entries = [
                {"alias": alias, "entity": alias}
                for alias in sorted({a for a in aliases if a and _ENTITY_RE.match(a)})
            ]
            devices.append({
                "key": key,
                "name": device.get("Name", ""),
                "entities": entity_entries,
                "commands": commands,
            })
        else:
            devices.append({"entity": key, "name": device.get("Name", ""), "commands": commands})
    return devices


def _normalize_power(state_str: str | None, available: bool) -> str:
    if not available or state_str in (None, "", "unavailable", "unknown"):
        return "unknown"
    low = str(state_str).lower()
    if low in ("off", "standby", "closed"):
        return "off"
    if low in ("on", "idle", "playing", "paused", "buffering", "open", "opening", "closing"):
        return "on"
    return "unknown"


def skip_reason(
    action: str,
    state_str: str | None,
    available: bool,
    attributes: dict[str, Any] | None = None,
    theater: dict[str, Any] | None = None,
) -> str | None:
    """Why a webhook command needs no service call, or None to run it (§5.5).

    Only discrete commands whose end state can be verified from fresh state
    skip. Toggles (`toggle`, `button:POWER`), `turn_off` and anything with
    unknown/unavailable state always run — "turn everything off" must go
    through even when Home Assistant's state lags the real device.
    `theater` must be a fresh webOS observation (the caller passes None when
    stale or the TV is off).
    """
    if action == "turn_on":
        return "already on" if _normalize_power(state_str, available) == "on" else None
    if action.startswith("webos_input:"):
        port = action.split(":", 1)[1]
        if theater and _normalize_power(state_str, available) == "on" \
                and (theater.get("active_input") or {}).get("id") == port:
            return f"already on {port}"
        return None
    if action.startswith("select_source:"):
        source = action.split(":", 1)[1]
        if _normalize_power(state_str, available) == "on" \
                and (attributes or {}).get("source") == source:
            return f"already on {source}"
        return None
    if action.startswith("select_option:"):
        option = action.split(":", 1)[1]
        if available and state_str == option:
            return f"already on {option}"
        return None
    return None


def _normalize_playing(state_str: str | None, available: bool) -> str:
    if not available or state_str in (None, "", "unavailable", "unknown"):
        return "unknown"
    low = str(state_str).lower()
    if low in ("playing", "paused", "idle", "off"):
        return low
    if low == "standby":
        return "off"
    if low in ("on", "buffering"):
        return "idle"
    return "unknown"


def _entity_priority(entity: Entity) -> tuple[int, str]:
    """Priority order for resolving overlapping state roles (§3.4)."""
    domain, platform = entity.domain, entity.platform
    if domain == "media_player" and platform in ("webostv", "androidtv_remote", "androidtv"):
        return (0, entity.entity_id)
    if domain == "remote" and platform in ("androidtv_remote", "androidtv"):
        return (1, entity.entity_id)
    if domain == "media_player" and platform != "cast":
        return (2, entity.entity_id)
    if domain == "media_player":
        return (3, entity.entity_id)
    if domain == "select":
        return (4, entity.entity_id)
    if domain in ("light", "switch", "input_boolean", "fan", "cover"):
        return (5, entity.entity_id)
    return (6, entity.entity_id)


def device_state_snapshot(
    key: str,
    entities: list[Entity],
    seq: int,
    updated: int,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One device's state snapshot for POST /api/ha-state (design §5.1).

    Pure function: never invents state. If a field's entity is missing or
    unavailable, the field is None / 'unknown', never 'off'.
    """
    alias_map = aliases or {}
    ordered = sorted(entities, key=_entity_priority)
    available = any(e.available and e.state not in (None, "unavailable") for e in entities)
    entity_states: dict[str, str] = {}
    for e in sorted(entities, key=lambda item: item.entity_id):
        alias = alias_map.get(e.entity_id, e.entity_id)
        if not e.available or e.state is None:
            entity_states[alias] = "unavailable"
        else:
            entity_states[alias] = str(e.state)

    power = "unknown"
    for e in ordered:
        if e.domain in ("media_player", "remote", "light", "switch", "input_boolean", "fan", "cover"):
            candidate = _normalize_power(e.state, e.available)
            if candidate != "unknown":
                power = candidate
                break

    source: str | None = None
    volume: float | None = None
    muted: bool | None = None
    playing = "unknown"
    mp_activity: str | None = None
    remote_activity: str | None = None

    for e in ordered:
        if not e.available or e.state in (None, "unavailable"):
            continue
        attrs = e.attributes or {}
        if e.domain == "media_player":
            if playing == "unknown":
                playing = _normalize_playing(e.state, e.available)
            if volume is None and isinstance(attrs.get("volume_level"), (int, float)) \
                    and not isinstance(attrs.get("volume_level"), bool):
                vol = float(attrs["volume_level"])
                if 0.0 <= vol <= 1.0:
                    volume = round(vol, 4)
            if muted is None and isinstance(attrs.get("is_volume_muted"), bool):
                muted = attrs["is_volume_muted"]
            raw_source = attrs.get("source")
            if isinstance(raw_source, str) and raw_source.strip():
                src = raw_source.strip()
                if e.platform == "webostv":
                    if _is_input_source(src):
                        if source is None:
                            source = src
                    elif mp_activity is None:
                        mp_activity = src
                elif e.platform in ("androidtv_remote", "androidtv", "cast"):
                    if mp_activity is None:
                        mp_activity = src
                elif source is None:
                    source = src
            for attr_key in ("app_name", "app_id"):
                val = attrs.get(attr_key)
                if mp_activity is None and isinstance(val, str) and val.strip():
                    mp_activity = val.strip()
        elif e.domain == "select":
            opt = attrs.get("current_option")
            if not (isinstance(opt, str) and opt.strip()):
                opt = e.state if e.state not in ("unknown", "unavailable") else None
            if source is None and isinstance(opt, str) and opt.strip():
                source = opt.strip()
        elif e.domain == "remote":
            cur = attrs.get("current_activity")
            if remote_activity is None and isinstance(cur, str) and cur.strip():
                remote_activity = cur.strip()

    activity = remote_activity or mp_activity
    if power != "on":
        # When off or unknown, stale source/app attributes on an entity are not active.
        if power == "off":
            playing = "off" if any(e.domain == "media_player" for e in entities) else "unknown"
        else:
            playing = "unknown"

    snapshot = {
        "key": key,
        "available": available,
        "power": power,
        "source": source if available else None,
        "volume": volume if available else None,
        "muted": muted if available else None,
        "playing": playing if available else "unknown",
        "activity": activity if available else None,
        "seq": int(seq),
        "updated": int(updated),
        "entities": entity_states,
    }
    webos = next((e for e in ordered if e.platform == "webostv" and e.domain == "media_player"), None)
    if webos is not None:
        theater = webos.webos_theater if _normalize_power(webos.state, webos.available) == "on" else None
        snapshot["theater"] = theater
        # CEC labels are not app names. Foreground identity is authoritative.
        snapshot["source"] = (theater.get("active_input") or {}).get("label") if theater else None
        if theater and theater.get("active_input"):
            snapshot["activity"] = None
        elif not theater or not theater.get("foreground_app"):
            snapshot["activity"] = None
        sound = theater.get("sound") if theater else None
        snapshot["volume"] = sound["volume"] / 100 if sound and sound.get("volume") is not None else None
        snapshot["muted"] = sound.get("muted") if sound else None
        if not theater:
            snapshot["activity"] = None
            snapshot["volume"] = None
            snapshot["muted"] = None
    return snapshot


def build_state_payload(
    groups: list[DeviceGroup],
    entities: list[Entity],
    device_set: dict | None,
    seq: int,
    updated: int,
    theater_links: list[dict] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """The full {"states": [...]} payload for POST /api/ha-state."""
    sent_devices = (device_set or {}).get("devices") or []
    states: list[dict[str, Any]] = []

    if groups:
        kept_groups, _ = suppress_cast_twins(groups)
        by_entity: dict[str, Entity] = {
            e.entity_id: e for g in kept_groups for e in g.entities
        }
        # Single-entity devices ride in the same set beside merged ones, so
        # their entities must answer state lookups too, not "unavailable".
        for entity in entities:
            by_entity.setdefault(entity.entity_id, entity)
        for spec in sent_devices[:MAX_DEVICES]:
            key = spec.get("key") or spec.get("entity")
            if not key:
                continue
            aliases = {
                item["entity"]: item["alias"]
                for item in spec.get("entities") or []
                if item.get("entity") and item.get("alias")
            }
            member_ids = [
                item["entity"] for item in spec.get("entities") or [] if item.get("entity")
            ] or [key]
            members = [
                by_entity.get(eid) or Entity(eid, eid, available=False, state="unavailable")
                for eid in member_ids
            ]
            snapshot = device_state_snapshot(key, members, seq, updated, aliases)
            _attach_theater_links(snapshot, members, theater_links)
            states.append(snapshot)
        return {"states": states}

    by_id = {e.entity_id: e for e in entities}
    for spec in sent_devices[:MAX_DEVICES]:
        key = spec.get("entity") or spec.get("key")
        if not key:
            continue
        ent = by_id.get(key) or Entity(key, key, available=False, state="unavailable")
        snapshot = device_state_snapshot(key, [ent], seq, updated)
        _attach_theater_links(snapshot, [ent], theater_links)
        states.append(snapshot)
    return {"states": states}


def _attach_theater_links(snapshot: dict, members: list[Entity], links: list[dict] | None) -> None:
    webos_ids = {e.entity_id for e in members if e.platform == "webostv"}
    if not webos_ids:
        return
    snapshot["theater_links"] = [
        {"port": link["port_id"], "device_id": str(link["hub_device_id"])}
        for link in links or [] if isinstance(link, dict)
        and link.get("entity_id") in webos_ids
        and re.fullmatch(r"HDMI_[1-9]\d?", str(link.get("port_id", "")))
        and re.fullmatch(r"\d{1,20}", str(link.get("hub_device_id", "")))
    ]
