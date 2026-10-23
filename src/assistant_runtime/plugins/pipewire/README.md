# PipeWire hardware integration

This adapter family supports microphone input and speaker output using the user's existing PipeWire/Pulse socket. System requirements: `pactl`, `pacat`, and a running audio service; Bluetooth speakers also require BlueZ and prior pairing. Construction validates app/configuration without connecting hardware. Installation does not change codecs or system audio ownership.

## Configure

Use stable logical IDs under project TOML `core/devices`. A microphone has `adapter="pipewire"`, `kind="microphone"`, label/location, and `connection.match`, a unique stable fragment of the PipeWire source name. Source monitors are excluded. Zero matches means unavailable; multiple matches mean error. A speaker has `kind="speaker"` and exactly one of `connection.address` (Bluetooth MAC) or `connection.sink` (exact PipeWire sink name). Transient node numbers never appear in app/configuration.

Bind the fixed voice input with `input_device` and fixed output with `default_output`. For example:

```toml
input_device = "office_microphone"
default_output = "office"

[core/devices.office_microphone]
adapter = "pipewire"
kind = "microphone"
label = "书房麦克风"
location = "书房"
[core/devices.office_microphone.connection]
match = "stable USB identity"

[core/devices.office]
adapter = "pipewire"
kind = "speaker"
label = "书房音箱"
location = "书房"
[core/devices.office.connection]
address = "AA:BB:CC:DD:EE:FF"
```

Another supported endpoint requires app/configuration, not a new plugin or a room branch. `input_device` and `default_output` are snapshotted at startup and cannot change during the process. Old persisted room choices are ignored. Replacing a voice endpoint requires editing those settings and restarting; there is no runtime selector or hot reload. Additional core/devices stay independently discoverable tools and never replace the fixed core/conversation input/output.

### Optional hardware mixer volume

An ALSA-backed PipeWire endpoint can explicitly reset one hardware volume control.
Omit `connection.hardware_volume` to leave all hardware mixer controls untouched.
This is separate from Pulse/PipeWire channel volume and `minimum_volume`; it does
not amplify PCM, unmute the device or select another endpoint.

```toml
# Within a USB speaker's connection table:
sink = "exact PipeWire USB sink name"
hardware_volume = { control = "PCM Playback Volume", index = 1, percent = 100 }

# Within a USB microphone's connection table:
match = "unique stable USB source identity"
hardware_volume = { control = "Mic Capture Volume", percent = 100 }
```

`control` is the full ALSA control name shown by `amixer -c <card> controls`;
`index` defaults to 0 and distinguishes controls with the same name. `percent`
is an integer from 0 to 100. The card is derived from the resolved endpoint's
ALSA identity on each initialization, never a app/configured transient card number.
Only readable/writable integer controls are supported. Missing controls, unsupported
network/Bluetooth endpoints, command timeouts or unconfirmed writes fail explicitly
before delivering PCM; there is no system-default card or software-gain fallback.

The provider applies the app/configured level when opening an input/output stream.
The primary voice route also initializes its physical sink when it uses the private
AEC playback bus. With this option app/configured, a new voiced segment after at least
0.5 seconds without voiced output resets the hardware level before playback, even
when the stream remains open. No periodic enforcement runs while idle, and device
inspection remains read-only. Configuration changes require a service restart.

## Registration and state

`PipeWireDevices` validates protocol app/configuration, registers each enabled device once, and probes the physical inventory every three seconds. Shared state includes stable ID, label/location, capabilities, observed time, age and freshness. The default freshness limit is 15 seconds, app/configurable per device. Probes are read-only and do not record PCM, connect Bluetooth, change volume or wake voice. The existing audio route owns reconnection of the fixed input/output.

Microphones declare `audio.capture`; state includes whether the bound capture process is running. Speakers declare `audio.playback`, `audio.volume`, `audio.stop`; state includes voice-bound/route-ready, readable volume and mute. Device-targeted volume changes that speaker's hardware volume; the explicit main-session stop tool only stops the assistant's main playback. `available` proves a physical node exists; acoustic verification is a separate acceptance step. A hardware error or stale observation must not be reported as a current successful measurement.

CLI and Codex's device tool use the same directory:

```sh
personal-agent --app/config app/config/app/config.toml core/devices list
personal-agent --app/config app/config/app/config.toml core/devices inspect office_microphone
personal-agent --app/config app/config/app/config.toml core/devices inspect office
```

Device MCP exposes `list_core/devices()` and `inspect_device(device_id="office")`. Connection addresses/selectors remain private adapter app/configuration; AI receives logical identity, capabilities and actual state. Passive observations never start a core/conversation.

Status separates `written_audio_bytes` and `discarded_audio_bytes`, and exposes
`playback_suppressed`, `playback_manual_muted` and `local_input_speaking` for output
diagnosis. A remote quiet boundary releases interruption suppression even while
the local VAD remains active on background speech. Explicit manual mute still
blocks playback; receipt of RTP/PCM frames or display of a transcript alone does
not establish that speech was delivered or heard. Empty output from the buffered
resampler is ignored before delivery and cannot reset the remote quiet interval.

## Acceptance

### Independent input and wake benchmark

Stop the assistant explicitly before testing, then run from the repository:

```sh
.venv/bin/personal-agent --app/config app/config/app/config.toml stop
.venv/bin/python -m scripts.check_audio_input --app/config app/config/app/config.toml --guided --save-audio
```

The test never stops, starts or enables the service. It refuses to open audio while
the user unit or the management socket is active, and stops its own streams if the
service starts during the benchmark. Restart the assistant yourself when ready.
All measurement, local wake inference and orchestration live in `scripts/audio_check/`;
there is no test capability, audio tap, prompt RPC or test state in the runtime.
The script uses the existing `AudioInputs` / `AudioOutputs` registries and PipeWire
providers, pinned to app/configured `input_device` / `default_output`. It does not
change endpoint selection, Bluetooth profile or production DSP policy. Stream
opening honors any explicitly app/configured hardware mixer initialization above;
without that option, the benchmark does not change volume.
The independently captured input is raw PCM, not the service's processed audio.

Follow the spoken prompts; no forms or typed confirmations are required. Keep the
microphone fixed. The guide gives 15 seconds to reach each position: living-room
center, far living room, kitchen and study. It runs quiet and ordinary external
TV/music conditions, allowing 20 seconds to set the background sound. At each
position it captures three seconds of background, a ten-second ordinary reading,
and a twenty-second wake-plus-reading window. **Say each sentence once**:

- Ordinary reading: “今天上午天气不错，我准备整理书桌，然后看看窗外。”
- Wake test: say the app/configured primary wake phrase, immediately followed by the
  same sentence. Other app/configured wake aliases are not separately prompted.

After the spoken start cue, wait two seconds and speak. The background and ordinary
reading are negative wake samples; the wake reading is positive. FSMN runs locally
on each captured segment, using the unchanged installed model. There is no model
training, cloud inference, ASR, injected user turn or native voice connection.
Review recordings for intelligibility and completeness of the first sentence;
transcript accuracy and live wake acceptance are separate acceptance work.
Positions and sound conditions follow operator instructions, not sensor validation.
The report compares background energy at matching positions and flags missing noise
contrast. Energy alone cannot separate a human voice from competing TV speech.

Fixed Mandarin prompts use local Kokoro v1.1 (`zf_079`, speed 0.95), via the existing
sherpa-onnx voice dependency. Assets must already exist under
`<app/config.models>/test-tts/kokoro-int8-multi-lang-v1_1/`; the script does not download
models, install packages or fall back to robotic/system/cloud TTS. An owned worker
validates and atomically caches complete WAVs before playback. `--prepare-guide`
prepares this cache without capture or playback. `--text-only --guided` prints the
same instructions. PipeWire playback drains to EOF so short cues are not cut off.

A single-instance lock prevents concurrent benchmarks. Ctrl+C preserves completed
rows, closes only owned streams and leaves the service stopped. Reports are private
mode-0600 JSON under ignored `state/acceptance/audio-input/`; `--output` chooses a
new JSON file and never overwrites an existing one. WAVs are saved there only with
explicit `--save-audio`. Without `--guided`, capture lasts five seconds by default
(`--seconds 1..30`) and neither plays prompts nor loads a wake model.

Delivery gaps are application timing, not Bluetooth packet-loss measurements.
Digital zeros can be silence or hardware gating. No app/clipping does not establish
intelligibility, and these short negative samples do not establish a long-term
false-wake rate. Completing the workflow is not a quality pass.

### Device onboarding

```sh
.venv/bin/python scripts/check.py
.venv/bin/python -m scripts.verify_device_onboarding --app/config app/config/app/config.toml --disconnect
```

The explicit real-device script requires the installed service to be idle. It briefly stops that service, starts an isolated state/provider thread with automatic core/memory disabled, checks the shared directory via the actual management socket, captures mic PCM in core/memory, plays the existing authored chime, and correlates the physical mic signal against that chime. It tests Bluetooth output disconnection/recovery and real Codex → MCP → socket → directory queries. It reapp/stores the installed service in `finally`, writes private JSON evidence under `state/acceptance/device-onboarding/`, and saves no raw audio. Successful stream submission alone is not accepted as physical playback success.

For wired outputs omit `--disconnect` and separately document physical cable removal/recovery. The unit gate covers absent and ambiguous identities, old-state invalidation, freshness, registration ownership, unknown core/devices, timeouts and no output substitution. USB removal and full host reboot require separate real-device evidence; the script does not claim those occurred.

The capture, playback and AEC-reference streams explicitly target their resolved
node and set `node.dont-reconnect`, `node.dont-fallback` and `node.dont-move`.
These are PipeWire's [session-manager properties](https://docs.pipewire.org/page_man_pipewire-props_7.html):
device loss must fail rather than move the stream to another source/sink. The
application reconnects only the same app/configured device. Exact behavior on other
session-manager versions requires integration verification.

Both primary and independent consumers acquire streams through the shared input/output
registries. PipeWire explicitly permits concurrent capture streams and mixed playback.
Each stream owns its pinned process and lease; closing or canceling one must preserve
others, including voice on the same endpoint. A tool capture returns data only to its
consumer, never into the main keyword/core/conversation path. Extra stream support does not
by itself expose a room listening or speech-generation MCP action. Per-device volume
is coordinated independently through `AudioVolume` and does not select a voice route.

Framework acceptance (2026-10-15): two independent input streams and two output
streams ran through the registries against virtual PipeWire endpoints. Closing each
tool stream preserved its voice stream and resource lease; final shutdown released
all leases. Pin properties were present and the installed main microphone stream
was uninterrupted. This used no physical audio and does not certify new room
listening or playback features. Live stdio MCP inventory/query verification after
service restart preserved the app/configured voice binding and shared core/conversation.
The full development gate passed 284 tests, including device lifecycle regressions
in `tests/test_device_operations.py`.
