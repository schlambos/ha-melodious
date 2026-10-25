# Melodious for Home Assistant

Home Assistant entities as devices on a [Melodious](https://github.com/schlambos/harmony-hub-control) Harmony hub: a rooted Logitech Harmony Hub running offline. Each entity you pick becomes an ordinary Harmony device, with power, inputs and commands. It can then sit in activities, go on the remote's buttons, and be sent from the Melodious web and Android apps.

When one of its commands runs, the hub posts `{"device": <entity_id>, "command": <value>}` to this integration's webhook, which calls the matching service. No Home Assistant token lives on the hub; the webhook ID is the only secret.

Home Assistant can already control the hub through the built-in **Logitech Harmony Hub** integration. This one adds the other direction.

## Requirements

- A Melodious hub with the Home Assistant device writer (`POST /api/ha-devices`).
- A plain `http://` address for Home Assistant that the hub can reach on your network. The hub can't verify https certificates.

## Install

1. In HACS: ⋮ → **Custom repositories** → add `https://github.com/schlambos/ha-melodious` with type **Integration**.
2. Download **Melodious**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Melodious.** Give:
   - the hub's address and web port (8080);
   - its web UI login, if it has one;
   - the Home Assistant URL for the hub, for example `http://192.0.2.5:8123`.
4. Open the integration's **Configure** and pick the entities.

## What each entity gets

| Domain | Commands |
|---|---|
| `media_player` | PowerOn, PowerOff, VolumeUp, VolumeDown, Mute, Play, Pause, PlayPause, Stop, SkipForward, SkipBack (as supported), and `Input <source>` for each source |
| `light` | PowerOn, PowerOff, PowerToggle; BrightnessUp/Down when dimmable |
| `switch`, `input_boolean` | PowerOn, PowerOff, PowerToggle |
| `fan` | as `switch`, plus SpeedUp, SpeedDown |
| `cover` | Open, Close, Stop |
| `script`, `scene` | Run / Activate |
| `button`, `input_button` | Press |

- **When it updates the hub:** the device list goes to the hub on every start, when the options change, and when an entity's device would change. The hub saves nothing for an unchanged list.
- **A device keeps every command and input it had**, so a player that's switched off never loses inputs that an activity uses.
- **On a Harmony Touch, each device gets its own screen**: Devices, then the device. The hub builds it from the device's commands. A command named like a hard key goes on that key (VolumeUp, Mute, Play…), a light's BrightnessUp/Down go on the volume keys, and every other command is an on-screen button. The screen follows the device when its commands change. This needs a Melodious hub with Touch device screens (`POST /api/device-screen`); an older hub adds the device without a screen.
- **Removing an entity** takes its device and its Touch screen off the hub, unless an activity or button still uses it.

Design notes and the hub side are in [docs/HOME_ASSISTANT.md](https://github.com/schlambos/harmony-hub-control/blob/main/docs/HOME_ASSISTANT.md).

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```
