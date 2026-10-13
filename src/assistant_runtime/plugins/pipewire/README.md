# PipeWire hardware integration

This adapter family supports microphone input and speaker output using the user's existing PipeWire/Pulse socket. System requirements: `pactl`, `pacat`, and a running audio service; Bluetooth speakers also require BlueZ and prior pairing. Construction validates configuration without connecting hardware. Installation does not change codecs or system audio ownership.

## Configure

Use stable logical IDs under project TOML `devices`. A microphone has `adapter="pipewire"`, `kind="microphone"`, label/location, and `connection.match`, a unique stable fragment of the PipeWire source name. Source monitors are excluded. Zero matches means unavailable; multiple matches mean error. A speaker has `kind="speaker"` and exactly one of `connection.address` (Bluetooth MAC) or `connection.sink` (exact PipeWire sink name). Transient node numbers never appear in configuration.

Choose the voice input with `input_device` and default output with `default_output`. For example:

```toml
input_device = "office_microphone"
default_output = "office"

[devices.office_microphone]
adapter = "pipewire"
kind = "microphone"
label = "书房麦克风"
location = "书房"
[devices.office_microphone.connection]
match = "stable USB identity"

[devices.office]
adapter = "pipewire"
kind = "speaker"
label = "书房音箱"
location = "书房"
[devices.office.connection]
address = "AA:BB:CC:DD:EE:FF"
```

Another supported endpoint requires configuration, not a new plugin or a hardcoded room branch. Change `input_device` to select another mic; choose an output with the `room` command using its logical ID. A removed persisted output fails startup instead of silently changing rooms; choose it explicitly through instance state/configuration before restarting. Changing configuration requires a service restart; there is no hot reload.

## Registration and state

`PipeWireDevices` validates protocol configuration, registers each enabled device once, and probes the physical inventory every three seconds. Shared state includes stable ID, label/location, capabilities, observed time, age and freshness. The default freshness limit is 15 seconds, configurable per device. Probes are read-only and do not record PCM, connect Bluetooth, change volume or wake voice. The existing audio route owns reconnection and selected-output control.

Microphones declare `audio.capture`; state includes whether the selected capture process is running. Speakers declare `audio.playback`, `audio.select`, `audio.volume`, `audio.stop`; state includes selected/route-ready, readable volume and mute. Volume/stop refer to the assistant's selected output stream, not arbitrary audio played by other applications. `available` proves a physical node exists; acoustic verification is a separate acceptance step. A hardware error or stale observation must not be reported as a current successful measurement.

CLI and Codex's device tool use the same directory:

```sh
personal-agent --config config.toml devices list
personal-agent --config config.toml devices inspect office_microphone
personal-agent --config config.toml devices inspect office
```

Codex calls `device(action="list")` or `device(action="inspect", value="office")`. Connection addresses/selectors remain private adapter configuration; AI receives logical identity, capabilities and actual state. Passive observations never start a conversation.

## Acceptance

```sh
.venv/bin/python scripts/check.py
.venv/bin/python -m scripts.verify_device_onboarding --config config.toml --disconnect
```

The explicit real-device script requires the installed service to be idle. It briefly stops that service, starts an isolated state/provider thread with automatic memory disabled, checks the shared directory via the actual management socket, captures mic PCM in memory, plays the existing authored chime, and correlates the physical mic signal against that chime. It tests Bluetooth output disconnection/recovery and real Codex → MCP → socket → directory queries. It restores the installed service in `finally`, writes private JSON evidence under `state/acceptance/device-onboarding/`, and saves no raw audio. Successful stream submission alone is not accepted as physical playback success.

For wired outputs omit `--disconnect` and separately document physical cable removal/recovery. The unit gate covers absent and ambiguous identities, old-state invalidation, freshness, registration ownership, unknown devices, timeouts and no output substitution. USB removal and full host reboot require separate real-device evidence; the script does not claim those occurred.
