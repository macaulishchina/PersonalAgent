# V4L2 camera integration

Platform: Linux and Windows / Windows Server. Certification declares code support; physical devices and external services require separate acceptance. See the [module index](../../../../docs/reference/modules.md).

The adapter is registered when explicitly enabled. Choose an exact local device identity, or use the [go2rtc integration](../go2rtc/README.md) for a network gateway.

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
pa --config config/config.toml devices list
pa --config config/config.toml devices inspect office_camera
pa --config config/config.toml devices snapshot office_camera
# Saving requires an explicit output path:
pa --config config/config.toml devices snapshot office_camera \
  --output state/camera-preview.jpg
```

Without `--output`, CLI prints metadata and discards pixels. Pi uses the same
capability through `capture_image(device_id="office_camera")`; the tool
returns native MCP image content rather than a filename or JSON pixels. Voice is currently disabled; image requests use the configured conversation/execution path.

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
- Explicit visual requests transmit images to the configured model provider. The application does
  not save them locally by default; provider conversation retention is separate.
  Explicitly saved acceptance images stay under ignored `state/`.

## Repeatable acceptance

```sh
.venv/bin/python scripts/check.py
pa --config config/config.toml restart
.venv/bin/python -m scripts.verify_video_onboarding --device living_camera \
  --save-frame --restart
# Only with explicit authorization to send test images to the configured model provider:
.venv/bin/python -m scripts.verify_video_onboarding --device living_camera --execution
```

Real acceptance checks installed directory/socket routing, repeated fresh JPEGs,
independent decoding/nonblank pixels, microphone coexistence, unknown-ID errors,
service restart recovery, and (with --execution) actual Pi list/inspect/snapshot. It never plays
audio or changes output selection. Tests cover invalid identity/configuration,
unavailable devices, permission/transport errors, unsupported formats, malformed
images, concurrency, generation changes, timeout, oversized output, cancellation
and process cleanup. Automated fake reconnection is not physical USB hotplug
evidence; actual unplug/replug remains an operator test.

Necessary acceptance evidence stays under ignored `state/acceptance/v4l2-camera/`.
Device onboarding does not require generating an HTML report or another report website.

Reference: [FFmpeg V4L2 input](https://ffmpeg.org/ffmpeg-devices.html#video4linux2_002c-v4l2).


## Windows 本机摄像头

`v4l2` 在 Windows 自动选择 `directshow` 后端，为 Windows／Windows Server 提供按需截图。Linux 本机摄像头继续使用 `v4l2`，网络摄像头可通过 `go2rtc` 协议适配器连接网关。安装 `camera` extra，并启用 `devices`、`v4l2`。

先使用 `imageio_ffmpeg.get_ffmpeg_exe()` 返回的 FFmpeg 执行：

```text
ffmpeg -list_devices true -f dshow -i dummy
```

选择视频设备的 **Alternative name**，保留完整 `@device_pnp_...` 路径；不使用友好名称、默认设备或序号。

```toml
[devices.office_camera]
adapter = "v4l2"
kind = "camera"
label = "办公室摄像头"
[devices.office_camera.connection]
device_path = '@device_pnp_完整的实际设备路径'
width = 1280
height = 720
framerate = 15
input_format = "mjpeg"
timeout_seconds = 8
```

`input_format` 支持 `mjpeg`、`yuyv422`；设备必须实际支持所选格式和分辨率。参数不兼容时明确失败，不选择其他设备或格式。宽高上限为 1920×1080、帧率上限 30、超时上限 10 秒。

后台仅枚举设备，不持续取帧。截图通过公共设备操作入口按请求采集，一次只允许一个采集操作；JPEG 上限 1 MiB，尺寸和格式经过验证。超时、取消和关闭会回收所属 FFmpeg 进程；设备缺失或采集失败不自动重放。

Windows Server 需要摄像头驱动和对应账号的设备访问权限。当前验收覆盖真实 DirectShow 后端、缺失设备拒绝、输出验证及实例重启；本机未连接用于验收的摄像头，尚未进行真实画面、权限弹窗和 USB 插拔验收。

上游：[FFmpeg DirectShow](https://ffmpeg.org/ffmpeg-devices.html#dshow)。
