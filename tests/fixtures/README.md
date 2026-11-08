# LG input capture

`lg_external_inputs.json` is the owner's OLED65C4/webOS 24
`tv/getExternalInputList` response captured through HA's `webostv.command`
during Phase 6. Sanitised before saving: icons/URLs, unique IDs and physical
addresses removed; no serial numbers or MACs. CEC type 8 on HDMI 1 is the
actual observed value (do not force playback type 4). HDMI 2's missing video
signal is not evidence that the soundbar is off. HDMI 3 has a plug/URCU
configuration but reports disconnected and has no SIMPLINK device.
