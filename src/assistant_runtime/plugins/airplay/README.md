# AirPlay speaker integration

This optional adapter registers AirPlay/RAOP speakers in the same device directory as local microphones and Bluetooth speakers. It uses pyatv 0.18.0's public discovery, connect, audio and StreamReader APIs. MP3 encoding uses FFmpeg supplied by the imageio-ffmpeg wheel in the project's single environment. No receiver protocol, custom RTP clock or separate environment is implemented.

## Install and configure

```sh
uv sync --locked --extra voice --extra airplay --extra memory --extra dev
```

Enable `airplay` in the top-level plugin list before any TOML tables. Add an enabled speaker definition and pin the discovered RAOP identifier, not merely a display name or address:

```toml
plugins = ["pipewire", "airplay", "keyword", "codex", "memory", "gesture"]

[devices.homepod]
adapter = "airplay"
kind = "speaker"
label = "HomePod"
location = "客厅"
max_age_seconds = 45
[devices.homepod.connection]
host = "192.168.1.50"
identifier = "receiver-raop-identifier"
timing_port = 49160
control_port = 49161
```

`host` must be an explicit IP. Discovery queries only that host and rejects another identifier at the same address. `credentials` and `password` are optional private connection fields for receivers that require pairing; this adapter does not silently pair. The current HomePod reports pairing unnecessary. Keep real connection data in ignored instance TOML. The device's network name is reported, never renamed.

UDP timing/control ports are configured explicitly. The deployment must already allow the receiver's required network traffic; installation does not alter host firewall rules. Each receiver is an exclusive stream resource; an additional tool consumer receives busy while its stream is held. Configure distinct sender ports for receivers that may stream concurrently.

## Media contract and routing

Startup registers the device descriptor/probe in `host.devices`, and an `AudioOutputProvider` in `host.audio_outputs`. The PipeWire audio owner still captures the configured microphone and maintains its existing local/AEC reference bus. For a registered external endpoint it opens the provider's `PlaybackTransport` rather than sending to a physical PipeWire sink. A reference-only mirror feeds the existing AEC bus without playing through another room speaker. No HomePod branch is added to assistant dialogue or Codex tools.

PCM input is clocked 20ms mono PCM16. A bounded 75-frame encoder queue feeds FFmpeg; idle silence keeps the open upstream stream valid. pyatv handles receiver conversion, session/authentication and RTP timing. Stop closes the encoder, clears queued PCM and tears down the receiver session; a following answer opens a new transport. Nothing is replayed to another output when the receiver is missing. Library/receiver buffering adds latency beyond the root player queue; quick, natural interruption and echo cancellation for that delay remain separate experience tests.

Queries describe network availability, fixed voice binding, stream state, encoder queue and failure. RAOP volume getters may report a library cache/default, so `volume_percent` is unknown until an explicit request; its source is labeled `last_explicit_request`. Unknown receiver volume is not adjusted by the application. pyatv attempts to preserve the receiver's advertised initial volume. Device-targeted volume requests use the typed device operation path. With a live stream, the adapter applies volume through pyatv; while idle it saves a next-stream preference and returns `applied=false, scope=next_stream`. Main voice retains its configured volume floor except explicit mute. No speech gain or clipping layer is added.

## Query, bind and test

```sh
personal-agent --config config/config.toml devices inspect homepod
.venv/bin/python -m scripts.verify_device_onboarding --output-device homepod --tail-seconds 10 --speech-sample /path/to/short-test.wav
```

Adding a receiver does not alter `default_output` or the main conversation route. Queries through CLI/MCP remain available. To use this receiver as the fixed voice output, set its logical ID in `default_output` and restart; voice, MCP and CLI cannot switch it during the process. The acceptance command sets the receiver as the binding before starting its isolated test instance, without modifying the installed configuration.

The explicit acceptance script requires an idle installed service, uses an isolated provider/state context, verifies registration/query/capture, plays the authored chime and optional short speech, checks a physical microphone signal and actual byte submission, and confirms real Codex tool queries. It closes the stream, retains private metrics and restores the installed service. PCM is processed in memory, not saved as a raw recording. A successful stream request or an online descriptor alone does not prove audible playback.

Evidence for the current receiver is under `state/acceptance/airplay-homepod/`. The original `homepod-assistant` directory supplied verified receiver/protocol/UDP settings and a Chinese speech fixture; it is not a runtime dependency.

References: [pyatv streaming API](https://pyatv.dev/development/stream/), [supported RAOP features](https://pyatv.dev/documentation/supported_features/).

Opening a tool stream uses the shared output registry with an explicit device ID and
owner. It does not replace the configured primary output. AirPlay does not advertise
mixing: a second open preserves the live receiver session and returns busy. The caller
closes its own transport; only adapter shutdown may close the provider globally.
