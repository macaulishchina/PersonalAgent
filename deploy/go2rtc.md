# go2rtc Docker 设备接入指南

用于添加摄像头、处理 USB 插拔和迁移 Linux 主机。当前网关运行官方 go2rtc 1.9.14；网络摄像头通过网关协议接入，USB 摄像头还需要显式的设备映射和访问权限。添加设备时分别验证容器可见性、设备发现、实际画面和 AI 调用。

## 配置位置与服务端口

| 文件 | 用途 |
| --- | --- |
| `docker-compose.yml` | 网关镜像、网络、持久化、健康检查和重启策略 |
| `deploy/go2rtc.usb.compose.yml` | 可选的单台 USB 摄像头映射 |
| `.env` | 本机选择的 Compose 文件、稳定设备路径和权限组，Git 忽略 |
| `state/go2rtc/go2rtc.yaml` | 实际监听地址、授权和视频流配置，Git 忽略 |
| `state/go2rtc/access.txt` | 本机访问说明及口令，Git 忽略 |
| `deploy/go2rtc.example.yaml` | 新安装的配置示例，不用于覆盖现有授权 |

使用 Linux host 网络。Web/API 为 `0.0.0.0:8553`，RTSP 为 `0.0.0.0:8554`，WebRTC 为 `0.0.0.0:8555`，其中 WebRTC 使用 TCP 和 UDP。监听地址在实际 `go2rtc.yaml` 中设置；修改 Web 端口时，也要同步修改 Compose 的健康检查 URL。host 网络下不添加 `ports` 映射。

局域网 HTTP 和 RTSP 使用配置中的账号口令。当前 `api.local_auth: false` 允许本机回环 HTTP，用于健康检查和 SSH 转发。凭据仅保存在私密配置中。配置目录挂载为可写，使 go2rtc 原生页面的授权及视频流设置可以持久化；目录权限为 `700`，私密文件为 `600`。新配置省略空的 `streams`，首次添加时由 go2rtc 写入；1.9.14 无法正确修改 `streams: {}` 这样的行内空映射，会出现 HTTP 400 且只在内存中添加成功。

## 添加第一台 USB 摄像头

先在宿主机确认视频节点及身份：

```sh
ls -l /dev/v4l/by-id/
stat -Lc '%g' /dev/v4l/by-id/YOUR_CAMERA-video-index0
```

选择实际支持视频采集的节点。一个物理摄像头可能产生多个节点，其中有些仅用于元数据；`video-index0` 是当前绿联摄像头的采集节点，不保证适用于所有设备。优先使用 `/dev/v4l/by-id/` 下的唯一身份，避免把宿主机的临时 `/dev/video0` 编号当作身份。没有唯一身份时需另行选择明确的识别方式；`by-path` 跟随 USB 插口，换插口后需要调整路径。

项目根目录的私密 `.env` 示例：

```dotenv
COMPOSE_FILE=docker-compose.yml:deploy/go2rtc.usb.compose.yml
GO2RTC_VIDEO_DEVICE=/dev/v4l/by-id/YOUR_CAMERA-video-index0
GO2RTC_VIDEO_GID=44
```

`GO2RTC_VIDEO_GID` 使用上面 `stat` 得到的数字，不能假定所有主机都是 `44`。网关默认以 UID/GID `1000:1000` 运行；`group_add` 补充视频节点的设备组权限。需要改变运行用户时，在 `.env` 配置 `GO2RTC_UID`、`GO2RTC_GID`，并让该用户能够读写 `state/go2rtc/`。

USB 扩展将选定宿主机路径映射为容器内 `/dev/video0`，授予读写权限。go2rtc 视频源使用容器内路径。host 网络仅提供网络访问，本身不会把 USB 视频节点传入容器。

在项目根目录应用配置：

```sh
docker compose config --quiet
docker compose up -d go2rtc
```

打开 `http://SERVER_IP:8553/add.html`，使用 **FFmpeg Devices (USB)** 或 **V4L2 (Linux video)** 查看格式列表，再添加所需视频流。MJPEG、YUYV 等条目是同一摄像头的不同格式。

## 新增第二台 USB 摄像头

当前 USB 扩展只映射一台设备。只新增 `.env` 变量不会自动增加映射。为第二台设备选择独立的宿主机身份、容器路径和视频流名称，保留第一台的映射。

可以把额外映射保存在私密 `state/go2rtc/usb-extra.compose.yml`，例如：

```yaml
services:
  go2rtc:
    devices:
      - "${GO2RTC_VIDEO_DEVICE_2:?Set the second camera path}:/dev/video1:rw"
    group_add:
      - "${GO2RTC_VIDEO_GID_2:?Set the second camera group ID}"
```

在现有 `.env` 中增加第二台的稳定路径和实际权限组，并把 `COMPOSE_FILE` 扩展为：

```dotenv
COMPOSE_FILE=docker-compose.yml:deploy/go2rtc.usb.compose.yml:state/go2rtc/usb-extra.compose.yml
GO2RTC_VIDEO_DEVICE_2=/dev/v4l/by-id/SECOND_CAMERA-video-index0
GO2RTC_VIDEO_GID_2=44
```

这里的容器 `/dev/video1` 指向第二台摄像头的采集节点，与宿主机编号无关。运行 `docker compose config --quiet` 校验合并配置，再运行 `docker compose up -d go2rtc`。仅使用网络摄像头的主机不启用 USB 扩展。

## 换插口与重新插拔

同一台摄像头的唯一 `by-id` 路径通常不随 USB 插口变化，因此通常无需编辑 `.env`。插拔后，宿主机实际视频节点可能改变；现有容器映射可能需要重新建立。

先确认配置中的稳定路径仍然存在，再运行：

```sh
docker compose up -d --force-recreate go2rtc
```

重建容器会保留挂载目录中的授权和视频流配置。`docker compose restart` 不会应用修改后的 Compose 设备映射或权限设置。当前部署没有新增 USB 自动接入、热插拔自动重绑机制。

## 迁移 Linux 主机

复用相同的源码与 Compose，迁移私密 `.env` 和 `state/go2rtc/`，然后核对以下本机参数：

- 摄像头仍有可用的 Linux 视频采集节点，稳定路径对应目标设备。
- 视频设备组 ID、运行用户 UID/GID 和持久化目录所有权适合新主机。
- 新主机的网络摄像头地址可达，`8553`、`8554`、`8555` 没有端口冲突。
- Docker 能随开机启动；`restart: unless-stopped` 恢复未手动停止的容器，并重启异常退出的进程。明确停止的容器保持停止，直到再次启动。

校验配置后启动网关，并重新做设备发现和画面验证。不要用示例 YAML 覆盖迁移来的真实配置。

## 检查与排障

从项目根目录执行：

```sh
docker compose ps
docker compose logs --tail 100 go2rtc
docker compose exec go2rtc id
docker compose exec go2rtc ls -l /dev/video0
curl --fail http://127.0.0.1:8553/api/v4l2
curl --fail http://127.0.0.1:8553/api/ffmpeg/devices
```

| 现象 | 检查方向 |
| --- | --- |
| 宿主机有摄像头，容器里没有 | 检查 `.env` 的 `COMPOSE_FILE` 是否加载 USB 扩展，以及设备映射是否存在 |
| 容器里有节点，但发现接口报权限错误或没有视频格式 | 核对采集节点、实际组 ID 和容器补充组权限 |
| 插拔后失效 | 核对稳定路径，再强制重建容器 |
| 发现了格式，但取图失败或报设备忙 | 检查所选格式、分辨率及其他采集进程；协调同一摄像头的采集占用 |
| 网络摄像头列表为空 | 分别检查账号、地区、设备授权和厂商接口；USB 映射不能解决云端设备列表问题 |
| 添加流时报错，但当前临时能播放 | 检查实际 `go2rtc.yaml` 是否保存了视频流；未保存的临时流可能在重启后消失 |
| 容器健康，但摄像头不能使用 | 健康检查只验证 HTTP API 就绪，继续检查真实设备发现和取图 |

当前绿联摄像头已通过容器读写权限、两个发现 API、真实网页列表、助手实际取图和 Codex 看图检查；网关停止与恢复、助手服务重启后的取图也已验证。新增设备仍需验证自己的实际画面与恢复行为，已有设备的成功不能代替新设备验收。

## 与个人助手的接入边界

go2rtc 负责视频协议和媒体访问，个人助手仍由原用户服务运行。当前 USB 映射只提供视频，不接管已有 PipeWire 麦克风。助手视频输入统一使用 [go2rtc 适配器](../src/assistant_runtime/plugins/go2rtc/README.md)，以网关地址和稳定视频流名绑定设备，并通过原有 `capture_image` 工具取图。网关列表中出现新设备后，还需要在个人助手的 `devices` 配置中声明该摄像头。接入时继续遵循 [硬件接入标准](../ARCHITECTURE.md#hardware-onboarding-standard)；已有 go2rtc 摄像头协议通常只需增加网关流和设备配置。

参考：[Docker 设备映射](https://docs.docker.com/reference/compose-file/services/#devices)、[Compose 创建与重建](https://docs.docker.com/reference/cli/docker/compose/up/)、[go2rtc 固定版本文档](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/README.md)。
