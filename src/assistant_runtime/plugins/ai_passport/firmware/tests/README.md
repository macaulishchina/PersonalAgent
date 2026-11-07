# PPDemo 检查分层

- `test_lab_model.c`：主机 C 测试人工报告、损坏记录、导航及有界配网表单解析。
- `test_terminal_model.c`：终端按键、短按／双击不误发、录音序号与上限、UTF-8 分页及休眠条件。
- `test_package_firmware.py`：PPDemo／PPDemo-Zh／PA-Terminal 独立身份、镜像布局、拒绝 NVS 覆盖及构建证据。
- ESP-IDF 5.5.3：实际解析组件、编译、链接和镜像尺寸检查；不以语法检查代替。
- 真实 PP：依照 `../README.md`，测试声音、屏幕、按键、无线、NVS 和休眠恢复。

开发机未连接 PP。所有人工报告初始为 NOT TESTED，不自动冒充硬件 PASS。
