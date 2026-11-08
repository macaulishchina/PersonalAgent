# AI Passport 语音终端

PP 是 PA 的独立入口：每台配对设备有自己的持久会话，与 Web 上下文隔离。复用主对话
模型、Pi、工具、可选记忆和任务通知；设备不持有供应商 Key，也不运行 Agent。

**本次协议 2 重构的软件检查、构建和首次真机启动已通过，联网后完整体验仍待验收，生产服务未替换。** 已部署的旧版
0.1.1 证据见 [acceptance.md](docs/acceptance.md)，不能作为新版本验收。
完整目标、当前进度和硬件约束见 [清单](docs/terminal-rebuild.md) 与 [硬件研究](docs/hardware-platform.md)。

## 配置与接入

显式启用 `speech_io`、`aliyun_speech`、`ai_passport`，并满足 conversation、Pi、模型、
ai_usage 等声明依赖。tasks、notifications、memory 是可选能力，不会因 PP 隐式启用。
供应商凭据继续放私有环境，原始音频默认不保存。

```toml
[speech_io]
max_streams = 2
max_recording_seconds = 60

[aliyun_speech]
api_key_env = "DASHSCOPE_API_KEY"
capabilities = ["recognize", "synthesize"]
recognition_model = "fun-asr-realtime"
synthesis_model = "cosyvoice-v3-flash"
voice = "longanyang"
rate = 1.0

[ai_passport]
volume = 45
brightness = 65
text_size = 16 # 正文支持 16 / 18，状态栏与菜单不变
sleep_seconds = 120
keep_online = false
input_mode = "steer"
speech_mode = "brief"
voice = "" # 留空沿用语音供应商音色
rate = 1.0
offline_cache = true
show_images = true
max_connections = 2
```

设备云控使用同一配置后台和文件事务：设备列表点击“设备配置”，填写覆盖值，统一保存并
生效。Wi-Fi 最多五套，用优先级 1—5 依次尝试；密码单独编辑并脱敏，其他字段修改不会
丢失已保存密码。设备按字段版本应用配置，本地音量、引导／排队、保持在线不会因重连重置。
音色、语速和播报模式在一次播报开始时取快照，不在句子中途突然换音色。

## 正式交互目标

- 通过设备热点页面配置初始 Wi-Fi 和 PA 业务根地址，例如 `http://服务器:8047`。
- Wi-Fi 联网后显示八位配对数字，在 PA 后台填写设备名和数字；不再下载配对文件或 USB 写配置。
- 业务入口为 8047 的 `/entrances/ai_passport/`；9443 只分发固件。
- 长按讲话、松开即停，超限等待确认，默认丢弃。短按 OK 结束本条播报，没有继续听。
- 双击切换引导／排队；播报时上下调音量，其余上下直接浏览有界缓存及在线历史。
- 文字流式显示；供应商明确标记的完整回答句可以提前播报，不朗读思考或执行进展。
- 图片仅使用所属会话的有界缩略图；不下载原图，不开放任意 URL 或文件路径。
- 默认两分钟空闲休眠，支持手动保持在线；没有可靠的充电检测，不能自动判断插电。
- 任务结果只投递所属 PP；离线积压只展示待查看数量，夜间不主动补播。

PPDemo、PPDemo-Zh 保留独立质检应用。固件唯一源码位于本插件 firmware；Python wheel
不包含固件、工具链和构建产物。固件网站只负责分发和浏览器刷写。

[协议与可靠性](docs/protocol.md) · [验收与待办](docs/acceptance.md) ·
[固件构建](firmware/apps/pa_terminal/README.md)

全部功能与软件检查完成后，按[集中真机验收](docs/hardware-acceptance.md)使用同一候选
完成实际语音、按键、图片、恢复、100次交互及两小时稳定性，不逐项刷机验收小改动。
