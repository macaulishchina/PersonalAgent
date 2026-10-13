# V4L2 camera integration

Linux UVC cameras with an exact `/dev/v4l/by-id/*-video-index0` or
`/dev/v4l/by-path/*-video-index0` identity use the `v4l2` adapter. UGREEN Camera 2K
and other MJPEG/YUYV cameras share this driver; no host or brand is hard-coded.

## Install and configure

Use the existing project Python 3.12 environment:

```sh
uv sync --locked --extra voice --extra camera --extra memory --extra dev
# Keep --extra airplay if enabled. No system FFmpeg installation is required.
ls -l /dev/v4l/by-id/
```

The camera extra supplies FFmpeg and Pillow. Normal-user read/write access to the
video node is required; installation does not modify host permissions. Add
`v4l2` to `plugins`, then add an endpoint:

```toml
[devices.office_camera]
adapter = "v4l2"
kind = "camera"
label = "书房摄像头"
location = "书房"
[devices.office_camera.connection]
path = "/dev/v4l/by-id/usb-YOUR_CAMERA_SERIAL-video-index0"
width = 1280
height = 720
framerate = 15
input_format = "mjpeg"
timeout_seconds = 8
```

Video and audio from one USB camera are separate logical endpoints, owned by
V4L2 and PipeWire. Do not change microphone selection. Another supported camera
requires another device entry, not another driver.

## Shared capability and AI use

The adapter registers `video.snapshot` with `DeviceDirectory` and a provider with
`host.video_inputs`. One shared `DevicesPlugin` owns `VideoCapture(device_id)` and
routes by logical ID. `VideoFrame` contains JPEG bytes, dimensions, host receive
time and connection generation. Pixels never enter the generic event bus or
long-term memory; `device.captured` publishes metadata only.

```sh
.venv/bin/personal-agent --config config.toml devices list
.venv/bin/personal-agent --config config.toml devices inspect office_camera
.venv/bin/personal-agent --config config.toml devices snapshot office_camera
# Saving requires an explicit output path:
.venv/bin/personal-agent --config config.toml devices snapshot office_camera \
  --output state/camera-preview.jpg
```

Without `--output`, CLI prints metadata and discards pixels. Codex uses the same
capability through `device(action="snapshot", value="office_camera")`; the tool
returns native MCP image content rather than a filename or JSON pixels. Voice
delegates user-requested visual questions to Codex and speaks its actual result,
using the existing subscription/task thread.

## Limits and privacy

- The observer checks stable identity, access and supported format/size. Format
  enumeration opens the node without starting frame streaming. Availability is
  freshness-aware; `available` alone is not proof of image capture.
- Each request opens one fresh acquisition and closes it. Only one acquisition
  per camera is allowed, with no request queue, stale-image fallback, substitution
  or automatic conversation activation. Failed acquisition remains an error
  until a successful capture or physical reconnection.
- JPEG is limited to 1 MiB and 1920×1080; timeout is configurable up to ten seconds.
  Actual JPEG dimensions/format and connection generation are validated. MJPEG
  passes through; YUYV uses one encoder thread. Process output/error buffers are
  bounded; timeout, cancellation and shutdown reap the child.
- There is no continuous feed or default recording. Future gesture/continuous
  video features require an explicit bounded consumer and trigger policy.
- `captured_at` is host acquisition completion time, not sensor exposure time.
  Camera exposure/warm-up may affect the first frame.
- Explicit visual requests transmit images to Codex/OpenAI. The application does
  not save them locally by default; provider conversation retention is separate.
  Explicitly saved acceptance images stay under ignored `state/`.

## Repeatable acceptance

```sh
.venv/bin/python scripts/check.py
.venv/bin/personal-agent --config config.toml restart
.venv/bin/python -m scripts.verify_video_onboarding --device living_camera \
  --save-frame --restart
# Only with explicit authorization to send test images to Codex/OpenAI:
.venv/bin/python -m scripts.verify_video_onboarding --device living_camera --codex
```

Real acceptance checks installed directory/socket routing, repeated fresh JPEGs,
independent decoding/nonblank pixels, microphone coexistence, unknown-ID errors,
service restart recovery, and (with --codex) actual Codex list/inspect/snapshot. It never plays
audio or changes output selection. Tests cover invalid identity/configuration,
unavailable devices, permission/transport errors, unsupported formats, malformed
images, concurrency, generation changes, timeout, oversized output, cancellation
and process cleanup. Automated fake reconnection is not physical USB hotplug
evidence; actual unplug/replug remains an operator test.

Necessary acceptance evidence stays under ignored `state/acceptance/v4l2-camera/`.
Device onboarding does not require generating an HTML report or another report website.

Reference: [FFmpeg V4L2 input](https://ffmpeg.org/ffmpeg-devices.html#video4linux2_002c-v4l2).
