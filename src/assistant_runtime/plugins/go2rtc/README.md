# go2rtc camera integration

All deployed assistant video inputs use configured go2rtc streams. The gateway owns USB device identity, camera protocols, formats and transcoding. The assistant owns the logical device ID, bounded snapshot request and native MCP image delivery. The microphone remains on PipeWire.

## Configure

Install the `camera` extra in the existing environment and enable `go2rtc` in the configured `plugins` list. Follow the [Docker device guide](../../../../deploy/go2rtc.md) to start the gateway and declare its source streams. A camera endpoint refers to an already registered stream:

```toml
[devices.office_camera]
adapter = "go2rtc"
kind = "camera"
label = "书房摄像头"
location = "书房"
[devices.office_camera.connection]
base_url = "http://127.0.0.1:8553"
stream = "office_camera"
timeout_seconds = 8
```

Only a gateway origin and a stable ASCII stream name are accepted. Device queries never expose the gateway address, source URLs or credentials. For an authenticated remote gateway, also set `username` and an absolute `password_file` containing only the password; keep that file private. Local loopback access works with the current gateway's `local_auth: false`. Redirects are rejected and environment HTTP proxies are not used.

For the current USB camera, go2rtc's private configuration declares a native MJPEG source using the container's video node. Network cameras declare their RTSP/vendor sources there instead. Format and resolution are configured once in the gateway. The assistant accepts JPEGs up to 1 MiB and 1920×1080; configure larger sources to provide an appropriately sized snapshot. Snapshot timestamps are host completion times, not camera exposure times.

## Shared capability

The adapter registers `video.snapshot` with `DeviceDirectory` and a `VideoInputProvider` with `host.video_inputs`. CLI and `device` MCP reuse the existing `VideoCapture` route:

```sh
.venv/bin/personal-agent --config config.toml devices inspect office_camera
.venv/bin/personal-agent --config config.toml devices snapshot office_camera
.venv/bin/personal-agent --config config.toml devices snapshot office_camera --output state/preview.jpg
```

Codex calls `capture_image(device_id="office_camera")` and receives native image content. Web/voice keep the same conversation and logical camera identity. Explicit image replies use the existing private chat attachment path; no raw video recorder is added.

## Availability and failure behavior

The five-second observer queries only stream metadata, without the video-probing parameters that open a consumer. `available` means the gateway can list the configured stream; it does not establish physical camera availability. `capture_verified` requires a successful frame in the current observed source generation. An unchanged stream registration does not prove that a disconnected physical camera is back online.

Capture requests use `/api/frame.jpeg?src=STREAM` without the gateway cache option. JSON and image response sizes, overall capture time and JPEG dimensions are bounded. One acquisition per logical camera is allowed. A failed acquisition returns an error; there is no stale-image or alternate-camera fallback. Server error bodies are never copied into model-visible errors because they may contain source credentials.

Observed gateway failures or changes to reported producer URLs invalidate the source generation. Before and after image acquisition the adapter checks that generation/source; changed or canceled results are discarded. HTTP cancellation and shutdown close requests and owned sessions. A gateway restart without an observed outage or source change is not an independent physical-identity signal.

Go2rtc releases the snapshot consumer after producing a frame. Other configured consumers, such as an open browser stream, can keep the source active. The assistant's observer does not start acquisition or activate voice. Continuous perception requires a separately configured media consumer and trigger policy.

## Verify

```sh
.venv/bin/python scripts/check.py
.venv/bin/personal-agent --config config.toml restart
.venv/bin/python scripts/verify_video_onboarding.py --device living_camera --restart --restart-gateway
# When sending an image to Codex/OpenAI is authorized:
.venv/bin/python scripts/verify_video_onboarding.py --device living_camera --codex
```

The reusable acceptance checks repeated nonblank JPEGs through the installed socket, unknown-device errors, microphone/output coexistence, actual device MCP image delivery, gateway outage/recovery and assistant restart. `--save-frame` explicitly retains one private preview. Evidence stays under ignored `state/acceptance/video/`; no HTML report is generated. Physical hotplug and whole-machine reboot are separate operator checks.

Reference: [go2rtc snapshot API](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/internal/mjpeg/README.md).
