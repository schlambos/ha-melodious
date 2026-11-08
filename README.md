# Melodious for Home Assistant

Home Assistant entities as devices on a [Melodious](https://github.com/schlambos/harmony-hub-control) Harmony hub: a rooted Logitech Harmony Hub running offline. Pick a whole Home Assistant **device** and its entities merge into one Harmony device (power, volume, inputs, apps, transport and D-pad), or pick single entities. The result can sit in activities, go on the remote's buttons, and be sent from the Melodious web and Android apps.

When one of its commands runs, the hub posts `{"device": <key>, "command": <value>}` to this integration's webhook, which calls the matching service. No Home Assistant token lives on the hub; the webhook ID is the only secret.

It also works the other way round: the hub appears in Home Assistant as a `remote` entity, so automations can start activities and send any hub command, like the built-in **Logitech Harmony Hub** integration's remote.

## Requirements

- A Melodious hub with the Home Assistant device writer (`POST /api/ha-devices`) and spec v2 (`ha-devices-v2` in `GET /api/version`). An older hub still works with one device per entity.
- A plain `http://` address for Home Assistant that the hub can reach on your network. The hub can't verify https certificates.

## Install

1. In HACS: ⋮ → **Custom repositories** → add `https://github.com/schlambos/ha-melodious` with type **Integration**.
2. Download **Melodious**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Melodious.** Give:
   - the hub's address and web port (8080);
   - its web UI login, if it has one;
   - the Home Assistant URL for the hub, for example `http://192.0.2.5:8123`.
4. Open the integration's **Configure** and pick whole devices and/or single entities. The review step shows each merged device's entities and commands; untick anything you don't want on the hub.

## Merged devices

A whole Home Assistant device becomes **one** Harmony device. Its entities are merged with a profile keyed on the entity registry's integration (`webostv`, `androidtv_remote`, `androidtv`, `cast`, …), not on the entity ID. The first command wins each canonical name, and a same-named Cast twin is suppressed before the merge.

### LG webOS TV

An LG webOS TV (`webostv`) gets:

| Command | What it does |
|---|---|
| `PowerOff` | discrete `turn_off` |
| `PowerToggle` | `webostv.button` `POWER`; used as the activity power command when there is no discrete `TURN_ON` |
| `VolumeUp` / `VolumeDown` | `volume_up` / `volume_down` (or the TV's `VOLUMEUP` / `VOLUMEDOWN` buttons when only `VOLUME_SET` is reported) |
| `Mute` | `volume_mute` toggle |
| `Play`, `Pause`, `PlayPause`, `Stop` | the matching media_player services |
| `SkipForward` / `SkipBack` | next/previous track (channel up/down while Live TV is showing) |
| `Input HDMI 1`, `Input HDMI 4`, `Input Live TV` | `select_source`; only real inputs become inputs |
| `App <name>` | `select_source` for every other source (Netflix, YouTube, …) |
| `DirectionUp` / `Down` / `Left` / `Right` | `webostv.button` `UP` / `DOWN` / `LEFT` / `RIGHT` |
| `Select` | `ENTER` |
| `Back` | `BACK` |
| `Home` | `HOME` |
| `Menu` | `MENU` |
| `Guide`, `Info`, `Exit` | `GUIDE`, `INFO`, `EXIT` |
| `0`–`9`, `Dash`, `Record` | the matching digit/`DASH`/`RECORD` buttons |
| `Red`, `Green`, `Yellow`, `Blue` | the colour keys |
| `ChannelUp` / `ChannelDown` | `CHANNELUP` / `CHANNELDOWN` |

- **Power honesty:** with no `TURN_ON` bit there is no discrete `PowerOn`. The activity uses `PowerToggle` (the TV's own `POWER` button) and `PowerOff` stays available for manual use. If you enable Wake-on-LAN in Home Assistant's webOS configuration, the `TURN_ON` bit appears and the profile switches to discrete `PowerOn`/`PowerOff` automatically.
- **Powering on from off:** Home Assistant 2026.1.2's webOS integration refuses `webostv.button` while the entity state is `off`. If `PowerToggle` cannot turn the TV on from off on your setup, enable Wake-on-LAN in the webOS integration's Configure step so the `TURN_ON` bit appears; the profile then uses discrete `PowerOn`/`PowerOff`.
- **Volume:** if the TV's sound output is an external soundbar/ARC, its volume commands are still on the device; if the soundbar/AVR is the activity's volume role, untick the TV's volume commands in the review step.
- **No absolute volume.** Harmony commands are step-based; the profile never invents a `Volume 35` command.

### Google TV / Android TV

A Google TV (`androidtv_remote`) gets:

| Command | What it does |
|---|---|
| `PowerOn` / `PowerOff` | `turn_on` / `turn_off` on the `androidtv_remote` media_player (it sends `POWER` only when the TV is off/on) |
| `VolumeUp` / `VolumeDown` / `Mute` | the media_player services |
| `Play`, `Pause`, `PlayPause`, `Stop` | the media_player services |
| `SkipForward` / `SkipBack` | next/previous track |
| `DirectionUp` / `Down` / `Left` / `Right` | `remote.send_command` `DPAD_UP` / `DPAD_DOWN` / `DPAD_LEFT` / `DPAD_RIGHT` |
| `Select` | `DPAD_CENTER` |
| `Back` | `BACK` |
| `Home` | `HOME` |
| `Menu` | `MENU` |
| `0`–`9` | the digit key codes |
| `Dash` | `MINUS` |
| `Red`, `Green`, `Yellow`, `Blue` | `PROG_RED`, `PROG_GREEN`, `PROG_YELLOW`, `PROG_BLUE` |
| `App <name>` | `remote.turn_on` with the app as the activity, only when `activity_list` is non-empty |

- **No inputs.** A Google TV is a source, not a display; the LG selects the HDMI input.
- **No app launching when `activity_list` is empty.** If the TV reports an empty `activity_list`, it gets no `App …` commands. The profile never guesses apps from `app_name`/`app_id`.
- **No `Record`.** `androidtvremote2` 0.2.3 (HA 2026.1.2) has no `KEYCODE_RECORD`; `MEDIA_RECORD` is a different key, so it is deliberately not emitted.

The older `androidtv` (ADB) integration uses the same canonical navigation names with `androidtv.constants.KEYS`: `UP`, `DOWN`, `LEFT`, `RIGHT`, `ENTER`, `BACK`, `HOME`, `MENU`, and the colour keys. That key table has no digits, `Dash`, `Record`, `Guide`, `Info`, `Exit` or channel keys, so those are not emitted. Power, volume and transport come from its `media_player`. **The ADB profile is code-and-unit-test only; it has not been verified on hardware.**

### Cast twins

A Google TV or LG TV often appears twice in Home Assistant: the real integration's device and a separate **Cast** device entry for the same hardware. When you select the real `webostv`/`androidtv_remote`/`androidtv` device, a same-named Cast-only device is suppressed, including the LG's bracketed `[LG] webOS TV …` name. Cast remains a fallback only when no webOS/Android TV device is selected; a Cast device's `source_list` entries become `App <name>` commands, not inputs.

If you previously added the Cast twin as its own entity device, remove it from activities and buttons first. The hub keeps a device that is still referenced and reports it as retained; once nothing uses it, the next push removes it.

## What each entity gets

| Domain | Commands |
|---|---|
| `media_player` | PowerOn, PowerOff, VolumeUp, VolumeDown, Mute, Play, Pause, PlayPause, Stop, SkipForward, SkipBack (as supported), and `Input <source>` for each source (a webOS TV splits inputs and `App <name>`) |
| `remote` | navigation for `androidtv_remote`/`androidtv`: DirectionUp/Down/Left/Right, Select, Back, Home, Menu, digits and colour keys where the key table has them |
| `select` | `Input <option>` for each option, routed to `select.select_option` (AVR source selectors) |
| `light` | PowerOn, PowerOff, PowerToggle; BrightnessUp/Down when dimmable |
| `switch`, `input_boolean` | PowerOn, PowerOff, PowerToggle |
| `fan` | as `switch`, plus SpeedUp, SpeedDown |
| `cover` | Open, Close, Stop |
| `script`, `scene` | Run / Activate |
| `button`, `input_button` | Press |

- **When it updates the hub:** the device list goes to the hub on every start, when the options change, and when an entity's device would change. The hub saves nothing for an unchanged list.
- **A device keeps every command and input it had**, so a player that's switched off never loses inputs that an activity uses. A v1 entity device migrating to a merged device keeps its old commands, now routed through the entity alias.
- **Limits:** 128 commands and 32 inputs per device. Apps are last, so a 128-command trim drops apps first and reports the reason in the review step and the log. True inputs are preferred over apps.
- **Long values:** a value must fit 96 characters. A long entity ID or a long app name makes the builder use a stable short alias (`e<8hex>`) for that entity.
- **On a Harmony Touch, each device gets its own screen**: Devices, then the device. The hub builds it from the device's commands. A command named like a hard key goes on that key (VolumeUp, Mute, Play, Menu, Back, Select…), and every other command is an on-screen button. The screen follows the device when its commands change. This needs a Melodious hub with Touch device screens (`POST /api/device-screen`); an older hub adds the device without a screen.
- **Unticking an entity or device** in Configure takes its device and its Touch screen off the hub, unless an activity or button still uses it.
- **Removing the integration** leaves the hub's devices where they are, so re-adding it picks up where it left off. Untick everything in Configure first if you want them gone.

## The hub as a remote

Each hub gets one `remote` entity (for example `remote.melodious_hub_192_168_0_123`), on a device named after the hub:

- **State:** on while an activity runs, off on PowerOff. `current_activity` and `activity_list` are the hub's activities; `devices` lists the hub's device names; `last_activity` is the one `remote.turn_on` resumes. It refreshes every 30 s and after every action, and shows unavailable (never off) when the hub can't be reached.
- **`remote.turn_on`** starts `activity` (name or ID), or the last activity, or the first one. An activity that's already running isn't restarted. **`remote.turn_off`** runs PowerOff.
- **`remote.send_command`** needs `device` (the hub's device name or ID) and sends each `command` by the hub's command name: IR, Bluetooth and Home Assistant devices alike. `num_repeats`, `delay_secs` (default 0.4) and `hold_secs` work as in the built-in integration. A hold is a real press-and-hold on the hub; on a Home Assistant device it repeats the command, so a toggle can fire more than once.
- **Names** match exactly, then by ID, then case-insensitively. An unknown device or command is logged with the names the hub has, and nothing is sent. A command name is the hub's (`DirectionUp`, `Input HDMI 1`), not a key code (`DPAD_UP`).
- **Not supported:** `learn_command` and `delete_command`. Learn IR in the Melodious app.

```yaml
actions:
  - action: remote.turn_on
    target: {entity_id: remote.melodious_hub_192_168_0_123}
    data: {activity: TV}
  - action: remote.send_command
    target: {entity_id: remote.melodious_hub_192_168_0_123}
    data:
      device: LG webOS TV OLED65C4PUA
      command: Input HDMI 4
```

Every command the hub sends to a Home Assistant device also fires a `melodious.command_received` event (`hub`, `device`, `device_key`, `entity_id`, `command`, `action`, `value`), so a Harmony button can trigger any automation:

```yaml
alias: Harmony DirectionUp also starts the movie lights
triggers:
  - trigger: event
    event_type: melodious.command_received
    event_data:
      device: Living Room TV
      command: DirectionUp
actions:
  - action: light.turn_on
    target:
      entity_id: light.movie_lights
```

## Live device state

When the hub reports `ha-state` in `GET /api/version`, Melodious also pushes a live state snapshot (`POST /api/ha-state`) for every exposed Home Assistant device:

- **What it carries:** `power` (`on`, `off`, `unknown`), `source` (input name), `volume` (`0..1`), `muted`, `playing` (`playing`, `paused`, `idle`, `off`, `unknown`), `activity` (current app), monotonic `seq`, `updated` and per-entity state strings. Missing or `unavailable` entities become `null`/`unknown`, never `off`.
- **Cadence:** state changes coalesce with a 2 s debounce into one batched push for all devices, plus a 30 s heartbeat, and never push more than once every 2 s.
- **Volatile on the hub:** the hub keeps the snapshot in `/var/volatile/codex-ha-state.json` (RAM only, never flash) and marks it stale after 90 s without a push. Unloading or stopping Home Assistant sends no fake `off` snapshot; the Melodious web and Android apps show **State unknown** once the snapshot turns stale.
- **Older hubs** without `ha-state` in `/api/version` are skipped silently.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Design notes and the hub side are in [docs/HOME_ASSISTANT.md](https://github.com/schlambos/harmony-hub-control/blob/main/docs/HOME_ASSISTANT.md).
