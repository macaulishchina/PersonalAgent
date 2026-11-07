# AI Passport 语音终端

每台已配对 PP 持有独立、持久的 PA 会话，与 Web 上下文隔离。复用主对话模型、
Pi 执行、任务、可选记忆、通知和统一用量。固件不保存供应商 Key，不运行第二套 Agent。

当前是 **0.4.34 软件／固件候选，PP 真机待验收**，未更换生产服务或发布正式固件。
Linux 服务端已做隔离验证；Windows 服务端尚未验收。Windows 浏览器负责刷写和 USB 配置。

## 启用与配置

在当前实例的显式插件清单中添加 `speech_io`、`aliyun_speech`、`ai_passport`，保留原插件；
依赖包含 conversation、Pi、模型、ai_usage。tasks、notifications、memory 为可选：
没启用时不创建对应业务，不因 PP 隐式开启。插件配置可以在配置后台扩展插件页面编辑。

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
public_url = "wss://47.109.34.254:9443/pa/passport/stream"
nickname = "嘟嘟"
volume = 45
sleep_seconds = 120
keep_online = false
input_mode = "steer"
auto_speak = true
max_connections = 2
```

密钥仅放实例 .env 或配置后台的私有环境凭据中。以上 URL 是当前部署候选地址，
必须完成下述代理配置后才可访问。保存、应用和重启沿用统一流程，不自动改变生产实例。

## HTTPS 与配对

1. 同一固件 HTTPS 网站仅新增 `/pa/passport/pair` 和 `/pa/passport/stream` 路由；
   参考 [代理片段](docs/passport-proxy.conf)，不要反向代理整个配置后台。
2. 管理进程 management.toml 的 `trusted_proxies` 只添加实际 Nginx 出口 IP／网段；
   Nginx 覆盖 X-Forwarded-Proto。设备流拒绝浏览器 Cookie、Origin，使用独立设备凭据。
3. 配置应用后，后台 AI Passport 页面填写设备名，下载一次性配对 JSON。10 分钟有效；
   文件含设备凭据，只能交给自己的设备，配置后删除下载文件。
4. 通过固件站的 `/pp-setup.html` 在 Windows Chrome／Edge 写入 Wi-Fi、配对文件和公开 CA 证书。
   页面只经 USB 发送，不上传、不写浏览器持久存储；不能选择 CA 私钥。
5. 设备列表可查看名称、连接状态和绑定会话，撤销立即断开设备，已经接受的任务继续执行。

当前硬件没有可靠的充电检测信号，按已确认方案提供菜单“自动休眠／保持在线”。
默认空闲 2 分钟休眠；用户可持久切换保持在线。不能声称插电会自动切换。

## 交互与可靠性

- 长按 OK 讲话、松开提交，半双工；短按停止播放或打开菜单，双击切换引导／排队。
- 按下即本地缓存；短按、双击不调用云识别。设备掉帧／溢出／断线明确失败，不提交残缺内容。
- 转写前绑定目标执行和共享用量，最终转写才提交。收据持久保存，相同请求不重复执行。
- 等待澄清时优先回答；录音期间目标结束可作为新输入，目标换成另一执行时拒绝误补充。
- 断网不取消已接受业务；重连查收据和会话，不自动重发未知操作，不补播历史音频。
- 默认播报最多约 200 字的完整句段；全文按页看。继续听沿用该结果工作身份。
  段落播放中断后从本段重新开始，只有播放完成才推进位置，避免漏听。
- 任务结果仅投递源于该 PP 会话的任务，沿用通知所有权；收到、播放完、已读分开记录。
  离线积压只列出，夜间不主动播报，主动按“继续听”可以播放。
- 原始录音不落盘；文字、结果引用、用量和回执使用现有私有持久存储。

[协议与模块边界](docs/protocol.md) · [验收记录与待办](docs/acceptance.md) ·
[Windows 刷写、配置及真机步骤](firmware/apps/pa_terminal/README.md)

PPDemo 和 PPDemo-Zh 保留独立质检应用。固件源码只在本插件 firmware 目录维护，
Python wheel 排除固件、工具链和构建产物。固件网站仅分发和 USB 配置。
