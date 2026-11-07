# PPDemo：AI Passport 硬件体验与质检

这是独立 ESP-IDF 固件，不依赖 PA 服务、不调用模型，也不实现 PA 设备接入。
复用官方板级驱动，应用、界面及测试逻辑独立实现。

PA 交互终端为独立工程 [apps/pa_terminal](apps/pa_terminal/README.md)，与本页的
PPDemo／PPDemo-Zh 共用 BSP、构建环境及打包工具，不混合菜单。终端候选已构建，真机待验收。

## 能体验什么

| 项目 | 程序提供的证据 | 需要你确认／边界 |
|---|---|---|
| 屏幕 | 颜色、灰阶、对齐、背光图案 | 坏点、闪烁、边缘和亮度是否正常 |
| 三个功能键 | ADC 毫伏、按下／单击／双击／长按／释放计数 | 每个键对应是否正确，电源键不作为软件输入 |
| 扬声器和麦克风 | 三个音调、3 秒录音及回放、峰值和削波数 | 声音清晰、回放确为自己录音；不是语音助手 |
| 电池 | CW2017 电量、毫伏或明确读取错误 | 部分硬件可能没有电量计；不测充电电流或续航 |
| Wi-Fi | 扫描数量、手机配网、获得局域网 IP | 仅 2.4 GHz；得到 IP 不等于互联网可用 |
| 蓝牙 | BLE 连接、GATT 写入及回读／通知 | 无经典蓝牙音频；测试链路不提供生产加密鉴权 |
| Flash／NVS | 写入、提交、回读及开机计数 | 断电后验证报告和计数保留；不做整片破坏性测试 |
| 休眠／唤醒 | 2 秒浅睡、10 秒深睡及唤醒原因 | 深睡相当于重启；不测低功耗电流 |
| USB | 控制台命令及 JSON 导出 | 是原生 USB Serial/JTAG，不是 USB 声卡 |
| NFC | 手机读标签的操作提示 | 被动 NTAG213，MCU 没有 NFC 读写能力；不改标签 |

**没有任何项目因驱动返回成功就自动通过。** 每项体验结束后，由你标记 PASS、FAIL
或 NOT TESTED。报告只表示人工结论，不能用作出厂认证。录音只放 RAM；不会上传，
下一次录音、进入无线测试或重启会释放。仅结果和测试计数写入独立 `ppdemo` NVS 命名空间。

提供 PPDemo 英文版与 PPDemo-Zh 中文版，共用测试逻辑，显示区域为 240×320、圆角屏。
中文版包含中文菜单、动作、评价、状态和 Wi-Fi 配网页；错误代码、协议名及 JSON 机器字段保留原文。
字体按实际界面文字裁剪，随固件内置，无需联网下载。长报告在固定内容区域自动滚动。

## 按键与测试流程

1. 上／下：选择测试；OK 单击：进入。
2. 测试内上／下：选择动作；OK：运行。屏幕测试直接切换图案。
3. **长按 OK：结束测试并进入人工评价**。无线／音频正在运行时先请求停止，
   等所属工作退出后才进入评价，不强行销毁仍被使用的资源。
4. 评价页上键 PASS、下键 FAIL、OK 保持未测试；成功保存后回到首页。
   保存失败会明确提示并保留原结论。评价页长按 OK 放弃本次评价。
5. 首页长按 OK：总报告；报告页单击 OK：向 USB 输出 JSON；长按 OK：首页。

按键测试页普通按键只计数，长按 OK 结束。录音前选择 Record，录完选择 Play recording。
回放音量固定 45%，测试音约 0.8 秒，避免突然大音量。休眠只有明确选择并按 OK 才执行。
深睡前必须释放所有按键；交接失败需要重启的场景会在串口说明，不能继续使用已移交的驱动。

### Wi-Fi

进入测试后按 OK 才开启无线。屏幕显示临时热点 `PP-DEMO-xxxx`、随机密码、180 秒期限。
手机连接此热点（无互联网是正常的），浏览器打开 **http://192.168.4.1**，输入 2.4 GHz
SSID 和密码。PP 显示分配的 IP 或断连状态。不要将此本地测试网页映射到外网。

热点最多一个客户端；SSID／密码只进入 RAM，不写 NVS、不打印到日志。退出／超时关闭
HTTP 服务及无线。配网页仅接受发往临时热点地址的请求，不接受从新连接的局域网地址修改。
没有互联网测速、账号登录、自动重连或云端请求。进入无线会释放录音。

### BLE

进入测试后按 OK；使用能操作通用 BLE GATT 的手机应用扫描 `PP-DEMO`：

- 服务 `FFF0`。
- 向 `FFF1` 写入 1～64 字节测试文字。
- 读取 `FFF2` 得到相同字节，或先订阅其通知。
- 屏幕显示连接次数和成功写入次数。120 秒到期或长按 OK 停止。

不要传真实密钥或隐私数据。若退出清理无法确认，保留资源并阻止新测试，重启设备后再试。

### USB 与报告

用数据线连接电脑，串口终端选择实际端口、115200、8N1，输入 `report` 加回车。
`help` 也导出报告；没有任意命令执行入口。JSON 与 ESP-IDF 日志共用串口，
只提取以 `{"app":"PPDemo"` 或 `{"app":"PPDemo-Zh"` 开头的完整行。报告含版本、开机次数、十项人工结论、
UI 队列丢弃数和剩余堆内存，不含录音、Wi-Fi 密码或生产凭据。

## 本机稳定开发环境

Dell 已将共享工具链放在 `/home/macaulish/workspace/esp32-toolchain/`。
新开终端先执行 `source /home/macaulish/workspace/esp32-toolchain/activate.sh`，
再进入本目录运行下述标准构建命令。激活只影响当前终端，不修改 PA 的环境。

本机候选产物集中在 `/home/macaulish/workspace/pp-firmware/candidates/`，
按应用、版本、真实镜像 SHA256 区分；正式发布区与未来网站源码位置独立。
目录设计及网站初步预研见 [publishing-research.md](docs/publishing-research.md)。

## 开发、构建与刷写

固定 ESP-IDF **5.5.3**、ESP32-C3、8 MB Flash，无 PSRAM。官方 BSP 来源、哈希见
`upstream.json`、`bsp.sha256.json`，MIT 许可见 `LICENSE.folotoy`。不把工具链装入 PA 的 Python 环境。

激活官方 ESP-IDF 5.5.3 终端，在本目录执行：

```text
idf.py --version
idf.py build
idf.py -p PORT flash monitor
```

将 `PORT` 换成实际串口，例如 Windows 的 `COM5` 或 Linux 的 `/dev/ttyACM0`。
`flash` 写入本工程的 bootloader、分区表和 factory 应用，**不要执行 erase-flash**。
当前分区沿用官方 NVS／PHY／factory 8 MB 布局；刷入前确认自己的固件布局一致。
如旧固件布局不同，先用官方工具保存设备 Flash，不能把本工程的兼容布局当作数据迁移保证。
写入应用会替换设备当前功能；不会刷写 NFC 标签，不代表兼容未来 PA 协议。

也可在 Linux 使用隔离构建容器（不挂 USB、不需要启动 PA）：

```sh
docker run --rm -v "$PWD:/project" -w /project espressif/idf:v5.5.3 idf.py build
```

首次解析组件会生成本工程自己的 `dependencies.lock`，应在真实构建通过后保留锁文件。
不能用上游旧锁文件或宿主 C 编译冒充 ESP-IDF 构建成功。

### 中文版构建

默认构建仍是英文版。中文版使用独立构建目录与配置，不覆盖英文构建：

```sh
idf.py -B build/zh -D SDKCONFIG=build/zh/sdkconfig \
  -D 'SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.zh.defaults' build
python3 tools/package_firmware.py --build build/zh --output build/zh/ppdemo-zh-0.2.0.zip
```

中文字体来自锁定 LVGL 9.5.0 附带的 SourceHanSansSC，生成的 14/16/20 px 子集和 SIL OFL
许可位于 `main/fonts/`。无需额外字体工具即可构建；修改中文文案后，开发者用固定的
`lv_font_conv@1.5.3` 执行 `python3 tools/generate_fonts.py --converter /path/to/lv_font_conv`，
再运行 `python3 tests/test_localization.py` 检查字形覆盖和 printf 参数一致性。
Dell 的字体工具安装于 `/home/macaulish/workspace/esp32-toolchain/font-tools/`，与网站和 PA 依赖隔离。
英文和中文固件共用 `ppdemo` NVS 命名空间，切换语言不清空原来的人工报告。

## 编译后的交付包

构建通过后执行 `python tools/package_firmware.py`，生成 `build/ppdemo-0.2.0.zip`。
包内含三个实际镜像、对应 ELF／MAP、解析锁文件、构建配置和 SHA256 清单。
**不生成跨过 NVS 的整片合并镜像**，避免把空白填充也写到已有 NVS 上。

Windows 无 ESP-IDF 时，安装 Python 3.10 或更高版本（已有 Python 可复用），
解压交付包，在其目录打开 PowerShell。先在设备管理器的“端口（COM 和 LPT）”
确认 PP 对应的 COM 端口，再执行：

```text
py -m pip install esptool==4.13.0
py -m esptool --chip esp32c3 --port COM5 write_flash 0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 ppdemo.bin
```

将 `COM5` 换成实际端口；未安装 Windows Python Launcher 时用 `python` 替代 `py`。
首次先关闭占用串口的程序。这个命令保留 NVS 分区，
仍会替换当前固件。若需要保留原固件，可在刷写前先用同一工具的
`read_flash 0 0x800000 pp-original.bin` 保存完整 Flash 到你自己的电脑，文件可能含设备凭据，勿公开。

刷完后可打开串口：

```text
py -m serial.tools.miniterm COM5 115200 --echo
```

输入 `report` 加回车验证双向 USB 通信；`Ctrl+]` 退出串口终端。先退出再刷写，
避免两个程序占用同一端口。串口日志和屏幕均可用于定位问题。

## 检查与真机验收

主机无 SDK 也可以验证配置解析和人工报告规则：

```sh
cc -std=c11 -Wall -Wextra -Werror -Imain tests/test_lab_model.c main/lab_model.c -o /tmp/ppdemo-test
/tmp/ppdemo-test
```

具备 CMake 的主机也可 `cmake -S tests -B build/host` 后构建并运行 CTest。
硬件验收建议先显示→按键→USB→音频，再做电池、存储、无线，最后深睡。
每项记录实际观察和 PASS／FAIL；录音播放、无线反复进入退出至少各 3 次，
检查 UI 可继续操作、可停止、没有持续下降的可用内存。深睡后重做按键和声音，
核对报告仍在；物理电源键／充电行为需人工观察，不标为软件接口已经验证。

构建、静态检查和真机验收分开报告；PP 在另一台电脑，当前没有真实硬件验收结果。
生成文件 `build/`、下载组件及本机 `sdkconfig` 不进入 Python wheel 或 Git。
