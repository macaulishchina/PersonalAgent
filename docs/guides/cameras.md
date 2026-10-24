# go2rtc Docker 设备接入指南

适用平台：Linux。Windows／Server 当前不能启用 go2rtc 扩展。以下路径相对于仓库根目录；先完成[应用部署](deployment.md)，再按需接入摄像头。

用于添加摄像头、处理 USB 插拔和迁移 Linux 主机。当前网关运行基于 go2rtc 1.9.14 的 `xiaomi-onvif 0.1.0-beta.2` 固定镜像，以提供已经实测的小米云台控制 API；源码版本为 `ea9d4153117ae612addc108870e372ec2289d4d2`，镜像摘要在 Compose 中固定。它沿用现有 go2rtc 进程、端口和授权配置，不额外启动 ONVIF 桥接服务。网络摄像头通过网关协议接入，USB 摄像头还需要显式的设备映射和访问权限。添加设备时分别验证容器可见性、设备发现、实际画面和 AI 调用。

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

新增设备需分别验证容器读写权限、发现 API、实际画面、模型取图，以及网关停止恢复和应用重启后的取图。历史设备验证见[早期验收记录](../archive/validation-2026-10.md)，不能替代新设备或当前执行器验收。

### 小米 CS2 握手超时与主机防火墙

小米登录成功、取得视频密钥、摄像头可 ping 通，都不能证明本地视频握手已成功。
CS2 最初向摄像头的 UDP 32108 发送发现请求；摄像头可能从另一临时端口
返回。主机防火墙可能将这个回复视为新的入站连接并丢弃。即使最终视频使用
TCP，初始 UDP 握手仍然需要通过；host 网络也不会绕过主机防火墙。

先核对内核日志中的来源、目标和协议，避免仅凭超时更换驱动或重新授权：

```sh
journalctl -k --since '15 minutes ago' --no-pager --grep 'UFW BLOCK'
sysctl net.ipv4.ip_local_port_range
ip route get CAMERA_IP
```

若日志确认摄像头的 UDP 回复被拦截，经管理员确认后添加明确限定的规则：

```sh
sudo ufw allow in on LAN_INTERFACE proto udp from CAMERA_IP to SERVER_IP port EPHEMERAL_PORT_RANGE comment 'go2rtc camera CS2 replies'
```

这些占位符分别使用实际网卡、摄像头地址、服务器地址和本机临时端口范围。
规则仅针对指定摄像头，不能直接照抄其他主机的地址或端口范围；迁移或设备
地址变化时重新核对。添加规则后验证两个镜头的实际取图、AI 工具路径和
断连恢复。保留原有防火墙策略，不把修改防火墙作为自动设备发现的一部分。

参考：[上游确认的 UFW 拦截案例](https://github.com/AlexxIT/go2rtc/issues/2424)、
[CS2 握手实现](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/pkg/xiaomi/miss/cs2/conn.go)。

### 小米双摄镜头选择

在 go2rtc 1.9.14 的 `miss` 实现中，第一镜头的视频源不带 `channel` 参数，
第二镜头使用 `channel=2`。不要给第一镜头填写 `channel=0`：当前实现只判断
参数是否非空，这会让两个视频流都请求第二镜头。保存两个独立的流名称后，
实际抓取并对照画面，确认视角不同；时间戳或图片文件哈希不同不足以证明
它们来自两个镜头。镜头的固定/云台角色也应根据实际设备确认。

参考：[固定版本的镜头选择实现](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/pkg/xiaomi/miss/client.go)、
[小米视频源配置](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/internal/xiaomi/README.md)。

当前小米 C300 双摄已通过左右、上下实际转向对照验证：不带 `channel` 的
主镜头可转向，`channel=2` 是固定广角。实例中的稳定设备 ID 保持不变，
仅为已验证的云台端点增加 `connection.pan_tilt`，通过助手的 `move_camera`
和 `stop_camera` 工具使用。不要直接把扩展对所有小米流提供的控制接口当作
每个镜头都具有电机的证明。常看位置接口与该型号服务编号不一致，未启用。

参考：[云台扩展的固定源码](https://github.com/SincereXIA/xiaomi-onvif/blob/ea9d4153117ae612addc108870e372ec2289d4d2/internal/xiaomi/ptz.go)。

## 与个人助手的接入边界

短视频录制通过已配置的视频流建立临时 H.264 转换流，录完即关闭并删除，
不额外修改每台摄像头的配置。MP4 完成后由助手使用打包的 FFmpeg 整理为
可直接播放、拖动进度的文件，保存在本机私密聊天附件目录。录制只接受明确
请求，默认 10 秒、最长 60 秒；默认没有声音。`record_video` 返回附件引用，
Web 沿用登录保护的媒体地址，视频文件不进入静态工作目录站点。

go2rtc 负责视频协议和媒体访问，个人助手仍由原用户服务运行。当前 USB 映射只提供视频，不接管已有 PipeWire 麦克风。助手视频输入统一使用 [go2rtc 适配器](../../src/assistant_runtime/plugins/go2rtc/README.md)，以网关地址和稳定视频流名绑定设备，并通过 `capture_image` 工具取图。网关列表中出现新设备后，还需要在 `devices` 配置中声明该摄像头，并完成适配器文档中的验收；已有协议通常只需增加网关流和设备配置。

参考：[Docker 设备映射](https://docs.docker.com/reference/compose-file/services/#devices)、[Compose 创建与重建](https://docs.docker.com/reference/cli/docker/compose/up/)、[go2rtc 固定版本文档](https://github.com/AlexxIT/go2rtc/blob/v1.9.14/README.md)。
