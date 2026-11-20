# PP 开发架构

ESP32-C3 负责按键、采音、屏幕和扬声器。PA 插件验证设备身份、管理设备会话，将输入交给对话和语音能力。PassportSim 执行同一份固件，供开发者在浏览器或命令行验证界面和按键。开发新功能时，先确定状态由哪一端拥有，再决定需要传输的数据。

## 从按键到播放

1. ADC 三键共用一个输入。BSP 在 esp_timer 任务中报告按下和松开，`terminal_events` 给按键预留队列。松开 OK 同时通知采音停止。按键回调只做有界工作。
2. 终端主循环依次分派按键、PA 消息和链路状态，推进输入、浏览、缓存和屏幕状态。`navigation` 选择当前操作的归属。`terminal_model` 决定预收音、开始、松手、等待确认等动作。界面由当前状态生成一帧交给 `ui`，LVGL 控件不直接写网络或 NVS。
3. `terminal_audio` 的单个 worker 使用 ES8311 和共用 I2S 时钟。采音是 16 kHz 单声道 PCM16，每块 640 字节、20 ms；32 块约容纳 640 ms。网络握手或发送赶不上时，本次输入应显式中止，不能缺一段仍当作完整语音提交。
4. `network` 负责 Wi-Fi、校时、配对和 WebSocket。`connection.py` 按设备会话协调识别、确认、提交及播放，`protocol.py` 维护协议 2 的字段与线上字节限制。`display.py` 提供显示用的消息分页、历史索引和缩略图。输入回执用于重连后核对提交结果。
5. 播放使用 24 kHz 单声道 PCM16。PA 根据终端授予的字节额度发送。`playback_buffer` 容纳 8 KiB，每次最多取 1 KiB 给 I2S，写入成功才返还相应额度。每次启动或停止都会改变本地播放代号，即使 PA 连续片段复用同一个 generation，旧写入也不能给新片段增加额度。I2S 接受数据与扬声器播完仍有硬件延迟。空闲时 worker 等待通知；编解码器故障只报告一次，浏览、按键和配置可以继续使用。

## 新需求放在哪里

| 修改内容 | 首选模块 | 回归方式 |
| --- | --- | --- |
| 页面、按键规则、长按和确认 | 终端 `navigation`、`terminal_model`、主循环的按键分派，外观由 `ui` 绘制 | C 状态机、PassportSim 固件场景、三键真机 |
| 配对、云配置、本地偏好 | PA 的 `settings.py`、`repository.py`；固件 `settings` 拥有 NVS、`provision` 拥有热点表单 | NVS 故障注入、配对与回执回归 |
| 回复、历史、任务消息、图片 | PA 的 `display.py`、终端的 `timeline`、`notification_model`、`pictures`、`history_cache` | 分页、断网缓存、分块和旧图释放 |
| 音频格式、背压或中止 | `terminal_audio`、`playback_buffer`，服务端协议变更对应 `protocol.py` 和 `connection.py` | 字节窗口、真实 WebSocket、真机 I2S 延迟 |
| 硬件引脚、共享总线、休眠交接 | `components/bsp` 维护锁定来源的驱动；`components/pp_board` 组合驱动； PA-Terminal 与 PPDemo 各自先停用自己的工作任务 | 板级故障注入、两个应用的 IDF 构建、按键唤醒和实测电流 |
| USB 状态诊断 | `diagnostics` 组装状态，USB 只读输出，不经 PA 会话 | 状态字段、真机串口 |

## 内存、存储与停机

ESP32-C3 有 8 MB Flash，无 PSRAM。屏幕 DMA、音频 DMA、两端音频缓冲与 Wi-Fi/TLS 同时占用内部 RAM。估算新功能时要同时看常驻开销、瞬时分配和最大连续空闲块。正文分页，小图分块，历史缓存可从 Flash 读取。小型网络帧复用接收缓冲，超过复用阈值的文本直接把缓冲所有权交给主循环，消费后擦除并释放。加入新的整图缓冲、完整录音或阻塞回调前，应先用真机测量峰值。

网络和配置任务可以写设置。主循环持有的小快照只含显示和当前交互所需字段，没有 Wi-Fi 密码、设备密钥或 CA。空闲时检查版本，设置锁被占用就沿用上一份快照。持久化失败时保留原设置，屏幕上未提交的音量反馈与 NVS 已保存值分开处理。

设备准备深睡时，终端先校验输入和播放状态及按键释放，等待音频 worker 停驻和网络退出，再交给板级组件。板级交接负责 ADC 向低电平唤醒的切换、编解码器、共享 I2C 和显示的停用。进入不可逆步骤中途失败时记录具体阶段并重启。唤醒从正常启动流程重新初始化。

## 验证入口

固件站仓库执行 `python3 tools/pp_lab.py run unit integration candidate`。主机 C 测试覆盖故障和边界输入，集成测试使用真实 PA/Pi 和合成语音、模型服务，候选构建检查锁定工具链、固件映像与安装包。用 `pp_lab.py sim run --candidate CANDIDATE_RUN --case CASE` 在 PassportSim 验证固件操作路径。每个待验收版本单独记录实体按键、音频尾音、TLS 连接时堆峰值、休眠电流和唤醒结果。

详见[硬件平台说明](hardware-platform.md)、[线协议](protocol.md)和[硬件验收](hardware-acceptance.md)。
