# PipeWire hardware integration

This adapter family supports microphone input and speaker output using the user's existing PipeWire/Pulse socket. System requirements: `pactl`, `pacat`, and a running audio service; Bluetooth speakers also require BlueZ and prior pairing. Construction validates configuration without connecting hardware. Installation does not change codecs or system audio ownership.

## Configure

Use stable logical IDs under project TOML `devices`. A microphone has `adapter="pipewire"`, `kind="microphone"`, label/location, and `connection.match`, a unique stable fragment of the PipeWire source name. Source monitors are excluded. Zero matches means unavailable; multiple matches mean error. A speaker has `kind="speaker"` and exactly one of `connection.address` (Bluetooth MAC) or `connection.sink` (exact PipeWire sink name). Transient node numbers never appear in configuration.

Bind the fixed voice input with `input_device` and fixed output with `default_output`. For example:

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

Another supported endpoint requires configuration, not a new plugin or a room branch. `input_device` and `default_output` are snapshotted at startup and cannot change during the process. Old persisted room choices are ignored. Replacing a voice endpoint requires editing those settings and restarting; there is no runtime selector or hot reload. Additional devices stay independently discoverable tools and never replace the fixed conversation input/output.

## Registration and state

`PipeWireDevices` validates protocol configuration, registers each enabled device once, and probes the physical inventory every three seconds. Shared state includes stable ID, label/location, capabilities, observed time, age and freshness. The default freshness limit is 15 seconds, configurable per device. Probes are read-only and do not record PCM, connect Bluetooth, change volume or wake voice. The existing audio route owns reconnection of the fixed input/output.

Microphones declare `audio.capture`; state includes whether the bound capture process is running. Speakers declare `audio.playback`, `audio.volume`, `audio.stop`; state includes voice-bound/route-ready, readable volume and mute. Device-targeted volume changes that speaker's hardware volume; the explicit main-session stop tool only stops the assistant's main playback. `available` proves a physical node exists; acoustic verification is a separate acceptance step. A hardware error or stale observation must not be reported as a current successful measurement.

CLI and Codex's device tool use the same directory:

```sh
personal-agent --config config.toml devices list
personal-agent --config config.toml devices inspect office_microphone
personal-agent --config config.toml devices inspect office
```

Device MCP exposes `list_devices()` and `inspect_device(device_id="office")`. Connection addresses/selectors remain private adapter configuration; AI receives logical identity, capabilities and actual state. Passive observations never start a conversation.

## Acceptance

```sh
.venv/bin/python scripts/check.py
.venv/bin/python -m scripts.verify_device_onboarding --config config.toml --disconnect
```

The explicit real-device script requires the installed service to be idle. It briefly stops that service, starts an isolated state/provider thread with automatic memory disabled, checks the shared directory via the actual management socket, captures mic PCM in memory, plays the existing authored chime, and correlates the physical mic signal against that chime. It tests Bluetooth output disconnection/recovery and real Codex → MCP → socket → directory queries. It restores the installed service in `finally`, writes private JSON evidence under `state/acceptance/device-onboarding/`, and saves no raw audio. Successful stream submission alone is not accepted as physical playback success.

For wired outputs omit `--disconnect` and separately document physical cable removal/recovery. The unit gate covers absent and ambiguous identities, old-state invalidation, freshness, registration ownership, unknown devices, timeouts and no output substitution. USB removal and full host reboot require separate real-device evidence; the script does not claim those occurred.

The capture, playback and AEC-reference streams explicitly target their resolved
node and set `node.dont-reconnect`, `node.dont-fallback` and `node.dont-move`.
These are PipeWire's [session-manager properties](https://docs.pipewire.org/page_man_pipewire-props_7.html):
device loss must fail rather than move the stream to another source/sink. The
application reconnects only the same configured device. Exact behavior on other
session-manager versions requires integration verification.

Both primary and independent consumers acquire streams through the shared input/output
registries. PipeWire explicitly permits concurrent capture streams and mixed playback.
Each stream owns its pinned process and lease; closing or canceling one must preserve
others, including voice on the same endpoint. A tool capture returns data only to its
consumer, never into the main keyword/conversation path. Extra stream support does not
by itself expose a room listening or speech-generation MCP action. Per-device volume
is coordinated independently through `AudioVolume` and does not select a voice route.

Framework acceptance (2026-10-15): two independent input streams and two output
streams ran through the registries against virtual PipeWire endpoints. Closing each
tool stream preserved its voice stream and resource lease; final shutdown released
all leases. Pin properties were present and the installed main microphone stream
was uninterrupted. This used no physical audio and does not certify new room
listening or playback features. Live stdio MCP inventory/query verification after
service restart preserved the configured voice binding and shared conversation.
The full development gate passed 284 tests, including device lifecycle regressions
in `tests/test_device_operations.py`.
