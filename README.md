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
4. Open the integration's **Configure** and pick whole devices and/or single entities. The **Review merged devices** step shows each merged device's entities and commands; untick anything you don't want on the hub. It also has the *Link LG HDMI ports to hub devices* checkbox (display only; see [LG theater state and links](#lg-theater-state-and-links)).

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
| `Input HDMI 1` … `Input HDMI 4` | `webostv.command` → `tv/switchInput` by stable port ID, not the changing CEC label |
| `App <name>` | `select_source` for apps (Netflix, YouTube, Live TV, …), excluding HDMI/CEC names |
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
- **Stable HDMI ports:** the merged profile reads `tv/getExternalInputList` through HA's existing webOS connection. `HDMI_1` always controls HDMI 1, even when CEC renames it. Input keys and command names stay `HDMI 1` / `Input HDMI 1` so activities and existing command IDs survive. CEC names are display labels in theater state. Old misclassified `App …` port commands are retained and repointed to their port when identified, to preserve buttons. When the TV is off, the last device spec keeps its ports; no new ports or labels are guessed from `source_list`. First discovery needs the TV already on; this integration never turns it on to read it.

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

## Mirrored hub devices

Devices that exist only on the hub can be mirrored into Home Assistant as real devices. In **Configure**, tick **Mirror hub devices into Home Assistant**, pick the hub devices, and select any commands you don't want. Each enabled command becomes a tap-only `button` entity; the device shows the hub's manufacturer and model, and its identity is tied to the hub's stable numeric device ID, so renaming it on the hub keeps the Home Assistant entities.

- **Opt-in per device.** Nothing is mirrored automatically.
- **No loops.** Devices Melodious already pushed to the hub (the Home Assistant devices in the same Configure flow) are never listed, and a stored mirrored device that later becomes Home Assistant-backed is ignored.
- **Tap only.** A `button` press sends one command. To hold a command (for example a volume ramp), use `remote.send_command` on the hub's remote entity with `hold_secs`; the button's attributes say the same.
- **Full vocabulary.** Every command the device has on the hub is offered. Disabling a command in Configure removes its button on the next reload.
- **Power is optimistic.** A `switch` exists when the hub reports a power feature. It is on exactly when the device is a member of the running activity and off after PowerOff; `assumed_state` is always true because IR gives no confirmation. Discrete devices send their PowerOn/PowerOff commands, toggle devices send PowerToggle in both directions. Devices with no power feature (the Toping E30) get no switch.
- **Inputs are optimistic too.** A `select` lists the device's hub inputs and sends the matching `Input<Name>` command. The hub cannot know the real input, so the pick shows after a successful send and resets to unknown when the activity changes. An input with no matching command raises a clear error instead of sending a guess.
- **Activity membership is diagnostic.** Each mirrored device gets a diagnostic `Activity` sensor: the running activity it belongs to, or `Idle`, with `activity_id` and its role (`VolumeActivityRole`, `DisplayActivityRole`, …).
- **The hub device gets activity controls.** Alongside the `remote`, it has an activity `select` (activities plus PowerOff), one `button` per activity plus **Power Off**, and an `Activity` sensor whose `members` attribute maps device names to their roles.
- **Cheap polling.** The coordinator checks `/api/state?version=` every 5 s and only re-reads the full activity config and state when the hub's state version moves (an activity start or stop).

## Live device state

When the hub reports `ha-state` in `GET /api/version`, Melodious also pushes a live state snapshot (`POST /api/ha-state`) for every exposed Home Assistant device:

- **What it carries:** `power` (`on`, `off`, `unknown`), `source` (input name), `volume` (`0..1`), `muted`, `playing` (`playing`, `paused`, `idle`, `off`, `unknown`), `activity` (current app), monotonic `seq`, `updated` and per-entity state strings. Missing or `unavailable` entities become `null`/`unknown`, never `off`.
- **Cadence:** a state change is pushed straight away in one batch for all devices; changes that follow within 1.2 s are coalesced into the next push (the hub takes one per second). A 30 s heartbeat keeps the state fresh.
- **Volatile on the hub:** the hub keeps the snapshot in `/var/volatile/codex-ha-state.json` (RAM only, never flash) and marks it stale after 90 s without a push. Unloading or stopping Home Assistant sends no fake `off` snapshot; the Melodious web and Android apps show **State unknown** once the snapshot turns stale.
- **Older hubs** without `ha-state` in `/api/version` are skipped silently.
- **Redundant commands are skipped (Phase 4b):** the same fresh state guards the webhook. A discrete `PowerOn` (`turn_on`) for an entity that is already on, a `webos_input` for the HDMI port an LG TV is already showing, and a `select_source`/`select_option` for the source or option already selected make no service call. Toggles, `turn_off` and anything with unknown or unavailable state always run, and `melodious.command_received` still fires for skipped commands, so automations chained to the button are never lost.

### LG theater state and links

While a selected LG is on, three read-only webOS queries run every 5 s: external inputs, volume status and foreground app. Input commands and HA state/source changes also request an immediate refresh. Queries time out after 4 s, do not overlap, and stop while the TV is off/unavailable. A failed field becomes unknown rather than retaining its last successful value; observations expire after 8 s. Raw responses are never logged or stored: the parser allowlists port/CEC labels, types, vendors, connection/signal flags and sound data only. It discards icons/URLs, unique IDs and physical addresses; no serial/MAC services are called.

The LG snapshot adds `theater`: `active_input` (`id`, `label`), `ports` (`id`, `label`, `connected`, `signal`, `cec_name`, numeric `cec_type`, numeric `cec_vendor`), `sound` (`output`, destination `label`/`port`, TV-reported `volume`/`muted`, `external_control`), and `foreground_app`. The off/unavailable/expired value is `null`. `connected` and video signal are **not power state**. The ARC destination is labeled only when exactly one connected CEC audio system identifies it. Volume is TV-reported: independent tracking of changes made with the soundbar's own remote still needs hardware verification.

**Approved linking model (owner decision, Phase 6):** explicit, display-only `(LG entity, HDMI port) → existing hub device ID` links, stored in integration options, not on hub flash. In **Configure**, check **Edit LG HDMI device links**; after reviewing devices, select one or more existing hub devices for each port. Multiple targets let an IR device and its HA counterpart show the same observation. Links never merge devices, replace commands, change activity roles, or override an HA device's measured state. Removing all selections clears a link; normal device reconfiguration preserves links. The snapshot carries `theater_links` (`port`, `device_id`) even while theater state is unknown, so the web app can show the linked source as unknown. Missing/deleted target IDs never control anything.

The owner confirmed bidirectional SIMPLINK auto-power sync; that could support a **derived**, explicitly labeled power proxy in future. This minimal implementation deliberately leaves linked-device power unknown and reports only ARC/volume/connection/signal observations. No arbitrary CEC commands or independent soundbar power measurement are promised. Older web apps ignore the extra fields; theater display needs the Phase 6 hub web-app update.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

Design notes and the hub side are in [docs/HOME_ASSISTANT.md](https://github.com/schlambos/harmony-hub-control/blob/main/docs/HOME_ASSISTANT.md).
