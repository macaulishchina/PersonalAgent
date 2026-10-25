# go2rtc camera integration

Platform: Linux and Windows / Windows Server. Certification declares code support; physical devices and external services require separate acceptance. See the [module index](../../../../docs/reference/modules.md).

All deployed assistant video inputs use configured go2rtc streams. The gateway owns USB device identity, camera protocols, formats and transcoding. The assistant owns the logical device ID, bounded snapshot request and native MCP image delivery. The microphone remains on PipeWire.

## Configure

Install the `camera` extra in the existing environment and enable `go2rtc` in the configured `plugins` list. Follow the [Docker device guide](../../../../docs/guides/cameras.md) to start the gateway and declare its source streams. A camera endpoint refers to an already registered stream:

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

For a dual-lens camera, declare two camera endpoints with different stream names
and the same `physical_id`. Keep each endpoint's availability and capture state
independent. Use channel labels until the actual lens assignment is confirmed;
do not infer wide-angle or pan/tilt control from a channel number. Registering
video endpoints does not grant motor-control or microphone capabilities.

```toml
[devices.dual_camera_second]
physical_id = "office_dual_camera"
adapter = "go2rtc"
kind = "camera"
label = "双摄第二镜头"
[devices.dual_camera_second.connection]
base_url = "http://127.0.0.1:8553"
stream = "dual_camera_second"
```

Set the first endpoint's `physical_id` to the same `office_dual_camera` value.
An account's discovery list is separate from saved streams: verify that both
stream names are present in `/api/streams` and in the private gateway YAML.
The snapshot API uses go2rtc's FFmpeg integration to convert H264/H265 sources
on demand. Keep the native video source registered in the gateway, for example:

```yaml
streams:
  dual_camera_second: YOUR_NATIVE_CAMERA_SOURCE
```

This follows the upstream [MJPEG integration](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/internal/mjpeg/README.md).
Successful discovery or stream registration is not successful video acquisition.
If the video handshake fails, preserve that failure and complete the acquisition
check after the upstream connection is restored. If the assistant was explicitly
stopped, validate in a temporary camera-only runtime without restarting its
microphone, wake-word detector, playback or shared conversation.

For the current USB camera, go2rtc's private configuration declares a native MJPEG source using the container's video node. Network cameras declare their RTSP/vendor sources there instead. The assistant accepts JPEGs up to 1 MiB and 1920×1080. For larger sources, add optional `snapshot_width = 1280` and `snapshot_height = 720` to the endpoint's connection settings. These request resizing by go2rtc only during snapshots; the native video stream and other consumers keep their original resolution. Omit these fields for sources already within the bounds. Snapshot timestamps are host completion times, not camera exposure times.

## Shared capability

### Requested video clips

Every registered gateway camera exposes `video.record`. `record_video` records a
silent clip on explicit request, defaults to ten seconds and accepts 1–60 seconds.
The gateway creates a temporary named H.264 producer referencing the configured
stream, so MJPEG and H.265 sources work without per-camera recording settings.
The HTTP consumer is bounded to 64 MiB and the requested duration; source changes,
failures and cancellation discard incomplete output. Closing the consumer and
removing its owned temporary stream release gateway encoding.

The packaged FFmpeg only finalizes the completed local MP4 with `faststart` and
checks a decodable frame; it never receives hardware credentials or accesses a
camera directly. Files stay in private instance storage. The shared conversation
owner adopts completed clips into its media store, and the device MCP returns
metadata and attachment references without sending video bytes to the model.
Web displays the clip on the live tool message, outside collapsed details, with
playback, seeking, full screen and download. Playback uses the authenticated
attachment route with HTTP Range support. Unrequested continuous recording,
camera microphone capture and video uploads/model video analysis are not added.

```sh
pa --config config/config.toml devices record office_camera --duration 10
pa --config config/config.toml devices record office_camera --duration 5 --output state/clip.mp4
```

### Optional pan/tilt

The deployed gateway uses the pinned `xiaomi-onvif` distribution, whose local
PTZ API reuses its active Xiaomi video connection. Official go2rtc 1.9.14 does
not provide that endpoint. No ONVIF bridge or second camera login is required.
Enable control only on the endpoint physically verified to be movable:

```toml
[devices.office_camera.connection.pan_tilt]
axes = ["pan", "tilt"]
max_duration_ms = 500
```

Omit this table for fixed cameras. At most one control endpoint is allowed per
`physical_id`. The device directory then exposes `camera.pan_tilt` and the
verified axes, maximum duration, in-flight command state and lack of position
feedback. Device labels describe the lens; they never grant control.

`move_camera(device_id, direction, duration_ms=80)` and `stop_camera(device_id)`
use the shared `CameraMotion` route. Directions are left/right/up/down; every
move is limited to 500 ms or the lower configured maximum. An on-demand MP4
consumer holds the same gateway connection only for the command and is discarded
without decoding or recording. Completed moves send an explicit stop. Stop and
shutdown interrupt owned work; failures remain unconfirmed and never replay a
move. Concurrent moves are rejected. A delivered command does not establish a
measured angle, exact return position, or disable vendor automatic tracking.

```sh
pa --config config/config.toml devices move office_camera --direction left
pa --config config/config.toml devices stop-camera office_camera
.venv/bin/python scripts/verify_camera_control.py --device office_camera --fixed-device office_fixed --execution
```

The adapter registers `video.snapshot` with `DeviceDirectory` and a `VideoInputProvider` with `host.video_inputs`. CLI and `device` MCP reuse the existing `VideoCapture` route:

```sh
pa --config config/config.toml devices inspect office_camera
pa --config config/config.toml devices snapshot office_camera
pa --config config/config.toml devices snapshot office_camera --output state/preview.jpg
```

Pi calls `capture_image(device_id="office_camera")` and receives native image content. Web uses the same logical camera identity. Voice is currently disabled. Explicit image replies use the existing private chat attachment path; no raw video recorder is added.

## Availability and failure behavior

The five-second observer queries only stream metadata and the declared stream configuration, without the video-probing parameters that open a consumer. `available` means the gateway can list the configured stream; it does not establish physical camera availability. `capture_verified` requires a successful frame in the current observed source generation. An unchanged stream registration does not prove that a disconnected physical camera is back online.

Capture requests use `/api/frame.jpeg?src=STREAM` without the gateway cache option. JSON and image response sizes, overall capture time and JPEG dimensions are bounded. One acquisition per logical camera is allowed. A failed acquisition returns an error; there is no stale-image or alternate-camera fallback. Server error bodies are never copied into model-visible errors because they may contain source credentials.

Observed gateway failures or changes to declared source URLs invalidate the source generation. The URLs are read from the authenticated configuration API and retained only as a hash; active producer metadata omits them and its connection IDs/statistics are not stable identities. Before and after image acquisition the adapter checks that generation/source; changed or canceled results are discarded. HTTP cancellation and shutdown close requests and owned sessions. A gateway restart without an observed outage or source change is not an independent physical-identity signal.

Go2rtc releases the snapshot consumer after producing a frame. Other configured consumers, such as an open browser stream, can keep the source active. The assistant's observer does not start acquisition or activate voice. Continuous perception requires a separately configured media consumer and trigger policy.

## Verify

```sh
.venv/bin/python scripts/check.py
pa --config config/config.toml restart
.venv/bin/python scripts/verify_video_onboarding.py --device living_camera --restart --restart-gateway
# When sending an image to the configured model provider is authorized:
.venv/bin/python scripts/verify_video_onboarding.py --device living_camera --execution
```

The reusable acceptance checks repeated nonblank JPEGs through the installed socket, unknown-device errors, microphone/output coexistence, actual device MCP image delivery, gateway outage/recovery and assistant restart. `--save-frame` explicitly retains one private preview. Evidence stays under ignored `state/acceptance/video/`; no HTML report is generated. Physical hotplug and whole-machine reboot are separate operator checks.

Reference: [go2rtc snapshot API](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/internal/mjpeg/README.md).

## Windows adaptation status

Windows Server checks have exercised this PA adapter with local protocol fixtures and actual Windows filesystem/process APIs. The Windows support declaration includes this adapter; enabling it still requires valid configuration and dependencies. External services and real accounts/devices are separate acceptance boundaries.
The existing Docker guide covers a Linux gateway. Windows PA can address an independently deployed network gateway through HTTP; Linux USB mappings are not applied to Windows. Local Windows USB cameras use the `v4l2` plugin with its automatically selected `directshow` implementation. Video finalization and probing use the shared hidden, owned process launcher; cancellation reaps the probe and output is bounded.
