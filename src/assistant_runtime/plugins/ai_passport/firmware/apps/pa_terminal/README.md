# PA-Terminal 0.1.0 候选

独立 AI Passport 终端固件，中文、按键讲话、文字与语音回应；不是 PPDemo 质检菜单。
**已构建及主机测试，PP 真机待验收。不要把编译通过当作麦克风、播放、续航验收通过。**

## Windows 刷写与配置

网站发布属于单独上线动作。候选包准备好后，可先用本地解压包刷写：

1. Windows 安装 Python 与 esptool（`py -m pip install esptool`），确认设备 COM 端口。
2. 解压可信候选 ZIP，核对 manifest.json 中三段镜像的 SHA256。
3. 关闭占用串口的应用，在解压目录执行（COM5 替换成真实端口）：

```powershell
py -m esptool --chip esp32c3 --port COM5 --baud 460800 write-flash --flash-mode dio --flash-size 8MB --flash-freq 80m 0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 pa_terminal.bin
```

不执行 erase-flash、不写 NVS；更新默认保留网络、配对、模式和音量。若使用固件站，
确认 PA-Terminal 名称、版本、候选标记和镜像校验后再安装。网站中的 PPDemo 不会被替换。

4. 在 PA 配置后台启用并应用插件，下载 10 分钟有效的 PP 配对 JSON。
5. Windows Chrome／Edge 打开固件站 `/pp-setup.html`。必须可信 HTTPS；
   自签站点先导入公开 CA，不能忽略证书错误来承诺 Web Serial 可用。
6. 连接 USB，填 Wi-Fi，导入配对 JSON 和公开 CA，选择是否保持在线，确认写入。
   USB 页面不向服务器发送凭据。设备重启后核对“就绪”。
7. 完成后删除下载的配对 JSON。恢复出厂需在配置页单独确认，仅擦除 `pa_terminal`
   NVS 命名空间；服务端配对还需在 PA 后台撤销，不会删除会话或取消服务器任务。

浏览器未上线时不要尝试填写到别的网站。不要向聊天发送 Wi-Fi 密码、设备凭据或供应商 Key。
需要 Wi-Fi 联网及 NTP 时间同步才能验证 TLS；首次连接没有正确时间时显示“同步时间”。

## 按键

- 首页按住 OK 超过 200 ms：保留按下开始的声音并识别，松开发送。
- 短按 OK：播放中立即停播，其他状态打开菜单。
- 双击 OK（300 ms 窗口）：引导／排队切换，掉电保留。
- 上下键：消息翻页；菜单和任务结果列表选择。
- 菜单：继续听、查看任务结果、取消当前执行、音量、网络状态、
  自动休眠／保持在线、休眠和返回。列表末尾“更早的任务结果”继续分页。
- 停播不取消任务。中断后的“继续听”从未播完的本句段重新开始，不跳过内容。
- 默认空闲 120 秒休眠；收音、播放、等待前台回复时不自动休眠。
  充电状态暂不能可靠检测，请桌面使用时手动选择“保持在线”。
- 睡眠唤醒先等待按键释放，之后才接受新讲话，避免唤醒动作误提交。

## 编译

源码唯一位置为当前目录；BSP 仍复用 `../../components/bsp`，不得修改已锁定板级实现
来掩盖适配问题。ESP-IDF 5.5.3、LVGL 9.5.0、WebSocket 1.6.1，由 dependencies.lock 记录。

```sh
source /home/macaulish/workspace/esp32-toolchain/activate.sh
cd /home/macaulish/workspace/dell-voice-assistant/src/assistant_runtime/plugins/ai_passport/firmware/apps/pa_terminal
idf.py build
python ../../tools/package_firmware.py --build build --output build/pa-terminal-0.1.0.zip
```

ESP32-C3、8 MB Flash，无 PSRAM。上行 16 kHz、下行 24 kHz 单声道 PCM16；
有界队列、音频与网络独立任务，接收信用窗口限制为 8192 字节。TLS 不跳过证书校验。
中文包含常用 GB2312 字符、标点、ASCII，罕见字使用字库占位；不破坏 UTF-8。

首次硬件测试顺序与记录表见 [验收说明](../../../docs/acceptance.md)。
