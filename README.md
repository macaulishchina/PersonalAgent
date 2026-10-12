# 嘟嘟 · DELL 原生语音助手

在 DELL 上常驻运行，使用绿联摄像头麦克风与客厅/书房蓝牙音箱。**复用 Codex 原生 WebRTC 语音会话和已有 ChatGPT 订阅登录，不使用项目 API key，不依赖 Windows。**

Codex 自己负责实时语音理解、回答、任务委派和结果回传。本地使用无界面的 Chromium 接收 WebRTC 语音，由浏览器的 NetEQ 处理抖动和丢包补偿；PipeWire 管理设备，WebRTC AEC3 消除本机播放的回声，sherpa-onnx 仅检测唤醒词和人声活动。旧 SenseVoice 与 edge-tts 运行链路已移除。

## 使用

说“嘟嘟”后会先听到本地短提示音，表示已检测到唤醒词；它不代表网络语音连接已经完成。首次建立连接期间保留开头讲话，连接失败播放低音提示，并可在 status 查看原因。

唤醒一次后连续交流，支持中文口语、后台任务及插话。原生会话处理插话；本地只有在收到用户发言事件且最近确有收音活动时才清空待播声音，避免迟到事件截断新的回答。默认回答简短，任务完成必须依据 Codex 的实际结果。

- “停一下／别说了”：停止播报，任务继续；下一次讲话恢复交流。
- “取消任务”：中断当前 Codex 工作及该任务的后台命令。
- “结束对话／先这样吧／回头再聊”：关闭语音回到本地待机，后台工作继续。
- “切换到书房音箱／切换到客厅音箱”：只使用指定房间的音箱。
- “音量调到百分之八十”：明确调整音量。

待机未检测到唤醒词就不向远端发送麦克风音频、不回应普通讲话。会话期间任何人声刷新空闲计时；没有活动任务和播放且空闲 60 秒返回待机。现场声源区分不在本轮范围内。

当前默认客厅。连接或播放时，能读到音量且低于 `minimum_volume = 80` 才提高到 80%；较高音量不降低，读不到不调整。音箱断连不转播到其他房间。

播放使用模型音频原始电平。当前音箱硬件音量未通过蓝牙提供可读值，系统 80% 不代表音箱自身旋钮或按键也是 80%。

客厅小爱音箱已实测采用标准 SBC / 44.1 kHz；48 kHz 会产生不断累积的播放滞后。安装脚本为本机 WirePlumber 0.4 安装兼容配置，此配置影响当前账户的蓝牙编解码选择。

唤醒使用原始麦克风信号和 2025-12-20 中文关键词模型；会话输入使用现成的 WebRTC AEC3。输出保持原始电平，没有额外输出增益或限幅。

播放保留字词间的静音和轻声帧，直接交给 PipeWire 时钟调度，使用约 120 ms 的播放缓冲吸收短时抖动。收到静音帧不会延长会话。有效插话和明确停播会清空队列并停止播放器。

## 管理

```bash
cd /home/macaulish/workspace/dell-voice-assistant
.venv/bin/dell-voice status
.venv/bin/dell-voice doctor
.venv/bin/dell-voice logs
.venv/bin/dell-voice start
.venv/bin/dell-voice restart
.venv/bin/dell-voice halt       # 停止服务
.venv/bin/dell-voice sleep      # 结束语音会话
.venv/bin/dell-voice stop       # 只停播
.venv/bin/dell-voice cancel     # 取消后台任务
.venv/bin/dell-voice history
.venv/bin/dell-voice room living
```

`status` 中 `backend=native`、`voice_auth=chatgpt`、`transport=webrtc` 表示新实现。`mode` 为 standby、connecting、active 或 reconnecting。`wake_count` 表示本次启动实际检测到的唤醒次数；`capture_recent` 表示音频帧到达，`input_rms` 表示最近一帧输入幅度，均不能替代现场听感验收。

`webrtc_engine=chromium`、`echo_cancel_engine=webrtc-aec3` 表示当前音频引擎；`rtp_inbound` 显示丢包及浏览器补偿计数。`voice_connect_ms` 分项记录首次建连等待。

`playback_queue_ms` / `max_playback_queue_ms` 是收到模型 PCM 到交给播放器的当前/最大本地排队时间，不包含 PipeWire、蓝牙和物理扬声器时延。

`config.toml` 控制名字、唤醒词、空闲时间、音量下限和设备身份。`realtime_voice=juniper` 来自本机 v3 接口支持的音色；不要直接套用独立 API 的音色名称。修改配置后重启。

## 安装

Python 3.12；需要已登录 ChatGPT 的 Codex、PipeWire、WirePlumber、BlueZ、pactl、pacat。当前验证版本为 Codex 0.162.1，原生实时接口属于实验接口，升级后应重新验收。

```bash
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements.lock
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/playwright install chromium --only-shell
.venv/bin/python scripts/download_models.py
.venv/bin/python scripts/install_service.py
.venv/bin/dell-voice start
```

用户级 systemd 自动重启，DELL 已开启 Linger。服务不会设置 API 凭据；启动时要求 ChatGPT 登录，并阻止 `OPENAI_API_KEY` 传给 Codex 子进程。此方案使用订阅语音额度，不代表无限使用。

## 恢复与记录

SQLite 保存任务状态和必要文字记录。Codex 专用持久线程保存工作上下文。语音断线重建同一线程上的语音会话，不自动重发输入或重新执行任务；断线期间讲话可能需要重说。默认不保存原始麦克风音频，唤醒前缓存仅在内存中。

原生语音与 Codex 的交接直接由 App Server 管理。`native.py` 负责原生语音协议，`browser_audio.py` / `.js` 连接无界面浏览器与本地 PCM，`native_service.py` 负责本地生命周期，`stream_audio.py` 管理有界播放队列，`backend.py` 跟踪任务和本地控制。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`scripts/probe_native_webrtc.py` 使用既有合成 WAV 验证真实订阅语音，不播放音箱。`scripts/test_native_service.py` 会播放测试语音并创建隔离测试文件，运行前需停止已安装服务，完成后再启动。测试 WAV 保留在 `state/acceptance`，不需要安装旧合成工具。

`scripts/diagnose_playback.py` 在静音虚拟设备中测量 6 秒连续音频及 35 ms 到包抖动；不调用模型、不录麦克风。加 `--speaker` 会在所选房间播放低电平测试音，运行前停止服务，运行后重新启动。报告保存在 `state/diagnostics/playback-*.json`；输出监视器测量不能代替房间内听感。

[验收记录](VALIDATION.md) 区分固定样本、设备输出、现场听感和整机重启。尚未把程序测试通过当作与 ChatGPT 完全一致的体验保证。

`scripts/test_room_conversation.py` 和 `scripts/test_room_audio.py` 会真实播放并短暂回录客厅麦克风，仅用于用户授权的现场诊断。前者可加 `--interrupt` 检查“停一下”；样本经同一音箱模拟人声，不能替代真人唤醒成功率。运行前停止生产服务，结束后重新启动；报告与回录保存在权限受限的 `state/`。`scripts/analyze_room_chirp.py` 对比回录和数字输出的时序，不将房间频响导致的低幅度误判为播放中断。
