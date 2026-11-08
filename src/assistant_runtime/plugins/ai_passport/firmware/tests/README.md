# PPDemo 检查分层

- `test_lab_model.c`：主机 C 测试人工报告、损坏记录、导航及有界配网表单解析。
- `test_terminal_model.c`：终端按键、短按／双击不误发、录音序号与上限、UTF-8 分页及休眠条件。
- `test_package_firmware.py`：PPDemo／PPDemo-Zh／PA-Terminal 独立身份、镜像布局、拒绝 NVS 覆盖及构建证据。
- `test_byte_buffer.c`：按需分配、分配失败保留原数据、16 KiB 网络上限、USB 8191 字节上限及安全释放。
- ESP-IDF 5.5.3：实际解析组件、编译、链接和镜像尺寸检查；不以语法检查代替。
- 真实 PP：依照 `../README.md`，测试声音、屏幕、按键、无线、NVS 和休眠恢复。

PP 已连接 Dell，当前正在分项验收。主机测试和构建通过不能冒充硬件 PASS，
实际固件、串口和未完成项见 `../../docs/terminal-rebuild.md`。

协议2联网/配置模型检查（使用同一套固定SDK的cJSON源码，不复制第三方源码）：

```sh
cmake -S tests -B build/host \
  -DPP_CJSON_SOURCE="$IDF_PATH/components/json/cJSON/cJSON.c"
cmake --build build/host
ctest --test-dir build/host --output-on-failure
```

`network_model` 检查五网络顺序、三轮边界、HTTP/HTTPS业务根地址及凭据格式；
`settings_model` 检查字段版本、本地偏好不被重连重置、网络变更、无效候选不修改当前值、
紧凑存储往返。没有设置 `PP_CJSON_SOURCE` 时仍运行其余不依赖SDK的主机测试；
这不构成配置模型或NVS硬件验收。真实NVS断电、无线及配网页守卫仍在设备验收中验证。

新增 `text_layout` 验证实际字宽回调、完整行窗口、UTF-8及末尾跟随；
`timeline` 验证三项索引、两页LRU、反复翻阅仍有界、修订失效及身份不截断；
`image_model` 验证像素尺寸、分块顺序、版本、大小及完成边界。

需要真实硬件、合成供应商的隔离环境时，使用项目的
`scripts/check_passport_hardware.py --help`。该工具需要独立空目录及串口只读状态中的设备ID，
只允许这台设备自动配对；使用真实 PA/Pi，ASR为占位转写，TTS为短音调，不调用付费供应商。
临时LAN验收端口不替换正式8047。原始音频不保存，正常退出清理所属服务和进程。
对工具创建且标记过的私有实例，`--resume`保留同一模型模拟端点、配对及会话；不接受任意生产目录。
`--tone-seconds 2.4 --long-reply`用于真实按键、停播、音量及长正文分页，不代表真实供应商能力。
