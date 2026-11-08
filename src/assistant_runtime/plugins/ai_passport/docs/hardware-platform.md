# PP 硬件与实现约束

核查日期：2026-11-08。本文服务于 PA-Terminal 正式终端重构；Demo 只定义交互，
不证明硬件容量或性能。引脚唯一来源仍是 `firmware/components/bsp/include/bsp_pins.h`。

## 依据与版本

- BSP 锁定来源：FoloToy/ai-passport `488beda41309f052d81f08d0016d65a3fd85eb7e`，
  见固件 `upstream.json`；本地上游检出已有后续更新，不能把新指南中的变更误当成已导入。
- [上游对应提交硬件指南](https://github.com/FoloToy/ai-passport/blob/488beda41309f052d81f08d0016d65a3fd85eb7e/docs/hardware-design/AI_HARDWARE_DEVELOPMENT_GUIDE.md)。
- [ESP-IDF 5.5.3 C3 内存分配](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32c3/api-reference/system/mem_alloc.html)。
- [ESP-IDF 5.5.3 C3 休眠](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32c3/api-reference/system/sleep_modes.html)。
- 稳定工具链在 `/home/macaulish/workspace/esp32-toolchain/`；用 `activate.sh`，
  不依赖开发终端临时 PATH，不向 PA Python 环境安装串口/固件依赖。

## 已从源码确认的硬件

| 子系统 | 实际约束 | 对实现的要求 |
| --- | --- | --- |
| MCU | ESP32-C3，8 MB Flash，无 PSRAM | 流式 PCM、有界正文及缓存；同时测空闲堆和最大连续块 |
| 显示 | ST7789P3，240×320，RGB565，SPI2 80 MHz | 单缓冲局部刷新，不建全屏透明合成层 |
| LVGL | 当前 24 KiB 池，240×40 单 DMA buffer = 19,200 B | 原尺寸猫图不可解码；资源预转换入 Flash；不反复创建控件 |
| 三键 | GPIO0 / ADC1_CH0，外部 10 kΩ 上拉，分压判定 | 一份 ADC 所有权；不能当作三个 GPIO 或支持组合键 |
| 按键事件 | BSP 回调来自共享 esp_timer 任务 | 仅更新紧急信号/入队，不能网络、I2C、NVS 或锁 LVGL |
| 音频 | ES8311，I2S0 TX/RX 共用时钟 | 半双工；切采样率必须由单一音频 worker 串行完成 |
| I2S DMA | 每方向 6 descriptors × 240 physical frames | 双 slot 总线，不能按逻辑单声道估算全部 DMA 占用 |
| I2C | GPIO10 SDA / GPIO7 SCL，codec 0x18、电量计 0x63 | 复用 BSP 总线，禁止应用重建 |
| 电量 | CW2017 可选读取 | 读取失败显示未知，不能伪造 85%；不新增未知电芯 profile 写入 |
| 电源 | 功放控制脚 -1；无可靠插电检测接口 | 不能承诺插电自动常在线或软件完全切断功放 |
| USB | 原生 Serial/JTAG，GPIO18/19 | 保留诊断和刷写；不是 USB 声卡 |
| 无线 | 2.4 GHz Wi-Fi | 有序列表、最多三轮；连上 Wi-Fi 与 PA 可达分开 |
| 深睡 | GPIO0 ADC 交还数字输入后低电平唤醒；唤醒重启 | 等待按键释放，先停 worker 再释放外设；不从终端交接中返回聊天 |

### 音频事实与速率

- 上行 16 kHz / mono / PCM16：32,000 B/s；640 B 为 20 ms。
- 下行 24 kHz / mono / PCM16：48,000 B/s；1,024 B 约为 21 ms。
- 60 秒原始输入约 1.92 MB，远超可用内部 RAM，不能缓存完整录音。
- 当前录音队列 32×含640B条目约 20 KiB，最多约 640 ms；握手等待必须有界，
  超过预缓冲必须报错，不能丢头音后继续执行。
- `esp_codec_dev_open` 同格式复用，变格式需关闭再打开；BSP 已绕过依赖的状态陷阱。
- 麦克风模拟增益 30 dB，`no_dac_ref=true`；输出音量 0—100，与输入增益无关。
- 依赖 `esp_codec_dev` 的 I2S read/write 默认等待 1000 ms；其返回值没有验证完整字节数。
  正式终端需要 BSP 提供可取消、有界、验证完整读写的 PCM 路径，不能改下载的组件源码。
- 停止前最多只能容忍一个小 PCM 分块；播放 credit 不能等同于 DAC 实际播完。

### 0.1.1 基线中发现的问题（本次已完成软件改造，真机分项另验）

1. `main.c` 把按键和分配文本的网络消息混放8项队列，队满会丢 release。
2. `PP_BEGIN` 发送后直接 `ready=true`，未等待服务端 `input.ready`；失败阶段不明确。
3. 一组独立布尔值表示 capture/recording/finishing/waiting，`sleep_now` 未覆盖 finishing。
4. 显示每100ms重设所有 label；昵称和模式多占正文行；80字固定分页无法保证完整可见。
5. 当前继续听/USB配置/WSS-only/单Wi-Fi属于旧产品流程，应按新需求删除或替换。
6. 连接时间必须等待 NTP，即使未来 HTTP/WS 不依赖证书时间，也会被无限阻塞。
7. 需要逐项校验消息 request_id 和播放版本；不能让上一条转写更新下一条输入。

## 初始预算与实测记录

下列只作设计账目，不是硬件验收：LVGL池24KiB、显示DMA19,200B、
音频DMA、录音队列约20KiB、播放队列约8KiB、Wi-Fi/LwIP、各任务栈、协议解析、
小图工作区与历史缓存。必须通过 `idf.py size` 与实际运行共同确定总和及峰值。

每个测量写明固件 SHA、活动功能、heap free/min/largest、栈余量、音频水位、
采集/松开/停播时间。持续100次混合交互与两小时在线分别记录，不以合成测试替代。

当前 Dell 已识别串口：`/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_98:C3:77:F6:F9:84-if00`。
操作者已授予当前用户访问。串口启动日志可与JSON行交错，采集应寻找完整JSON对象，
不能只接受以JSON开头的整行而误报设备不响应。

### 2026-11-08 首次真机基线（旧固件）

`PA-Terminal 0.1.1 / protocol 1`；状态快照 heap=39,132 B、largest=25,600 B、
minimum=31,092 B，随后 `ws_connected` 阶段 heap=37,392 B、largest=23,552 B。
这是旧WSS路径连接快照，不是语音/图片并发压力数据。说明新功能没有数百KiB空闲空间，
不能简单叠加全图缓存或第二份PCM缓冲。

本次输入硬化候选已用固定SDK构建：静态DRAM 165,876 B，链接器报告剩余155,420 B，
镜像约2,014,320 B。链接器剩余空间不等于运行空闲堆，二者必须分别报告。
修改过的BSP文件列入 `upstream.json` adapted_files 和新的校验清单；
其变更是公开I2S读写检查完整字节数、按PCM时长+50ms有界等待、音量错误传播。

### 热点HTTP的双栈地址

当前SDK启用`CONFIG_LWIP_IPV6`，`esp_http_server/src/httpd_main.c`使用PF_INET6监听；
lwIP的`lwip_getaddrname`会把IPv4连接转换为IPv4映射IPv6地址。热点接口即可能以
`::ffff:192.168.4.1`返回，不能用`struct sockaddr_in`或仅AF_INET判断它是否来自热点。
接入使用`sockaddr_storage`，只接受普通IPv4或严格映射IPv6中的192.168.4.1，
仍校验Host/Origin。主机回归覆盖两种有效表示、截短地址、非热点地址及跨站来源。
