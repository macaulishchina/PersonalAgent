# PP 固件构建、管理与浏览器安装：初步预研

调研日期：2026-11-07。本轮整理本地环境、产物和设计边界，没有实现网站、发布固件、
启动监听端口或操作 PP。当前 PPDemo 只有主机检查和真实 ESP-IDF 构建证据，真机待验收。

## 官网和现有组件能做什么

官网已有“玩法目录→版本说明→安装”的用户流程；安装页强调 Windows 使用 Chrome／Edge，
用户主动选择 USB 串口后开始下载和写入。官网玩法安装和设备专属恢复是两个流程。
恢复出厂页可能带设备 SN／KEY，不能把这种私有链接混进我们自己的公共固件目录。

- [官网安装说明](https://ai-passport.folotoy.cn/guides/flash-official-firmware/)
- [官网浏览器刷写入口](https://ai-passport.folotoy.cn/tools/web-flasher/)
- [官网恢复说明](https://ai-passport.folotoy.cn/guides/restore-factory-firmware/)

浏览器安装通过用户电脑的 Web Serial 访问 USB，不需要服务器接 USB，也不要求固件先联网。
网站仍须下载固件文件；这与设备自行联网 OTA 更新不同。页面应在点击后请求串口权限，
对支持能力和安全上下文做实际检测。初期以桌面版 Chrome／Edge 为验证范围。
普通局域网 HTTP 地址不满足安全上下文要求；正式访问使用 HTTPS，本机开发可用 localhost。

- [Chrome Web Serial 文档](https://developer.chrome.com/docs/capabilities/serial)
- [安全上下文规则](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts)

## 已确认优先复用 ESP Launchpad 整站

进一步核查后，选择优先复用乐鑫公开的 ESP Launchpad 前端，而不是从底层刷写组件开始自建网站。
项目采用 Apache-2.0，提供可自行构建部署的完整前端；应用与镜像通过 TOML 登记。
用户已确认“发布”只需把固件放入文件服务器、维护清单，不要求管理员上传后台。
因此这一方式已覆盖当前的添加、展示、选择和安装需求。

| 方案 | 已有能力 | 当前选择 |
|---|---|---|
| ESP Launchpad | 可配置应用目录、说明、分段镜像、网页刷写和串口控制台 | 优先复用整站；将来补本地发布命令生成文件和 TOML |
| ESP Web Tools | 嵌入式安装按钮、清单和终端 | 备用组件；擦除及合并镜像行为需核对 |
| esptool-js | 多文件地址、Flash 参数、擦除控制及协议传输 | Launchpad 的底层能力，不另行重写刷写协议 |

- [ESP Launchpad 源码与部署说明](https://github.com/espressif/esp-launchpad)
- [Launchpad 分段镜像配置](https://github.com/espressif/esp-launchpad/blob/main/config/minimal_launchpad_config.toml)
- [esptool-js 与分段／稀疏镜像说明](https://github.com/espressif/esptool-js)
- [FlashOptions 参数](https://espressif.github.io/esptool-js/docs/interfaces/FlashOptions.html)
- [ESP Web Tools 清单与擦除规则](https://esphome.github.io/esp-web-tools/)

初步代码核查证明 Launchpad 可以配置多个镜像及各自地址，能够衔接 PPDemo 的三个镜像；
后续需锁定实际复用版本，验证校验、擦除、断连及重启语义，不能把源码支持当作真机验收。

安装前应校验下载文件大小和 SHA256、芯片兼容性、Flash 容量、分区与写入区间，显示影响，
再由用户主动安装。`eraseAll: false` 仍会擦除被写入文件覆盖的扇区，不能宣传为“绝不会丢数据”。
分区布局或数据格式发生变化时，需要单独说明；芯片识别为 ESP32-C3 也不能自动证明设备就是 PP。
下载、传输写入校验、重启和应用启动应分别显示，不能仅凭进度到 100% 认定全部正常。

## 本地目录：共享工具、唯一源码、明确产物

```text
/home/macaulish/workspace/
├── AI-Passport/                         官方参考源码；保持独立
├── esp32-toolchain/                     本机稳定工具链；不放应用源码
│   ├── esp-idf-v5.5.3/                  固定 SDK 与子模块
│   ├── tools/                          官方 IDF 工具管理器布局
│   ├── python_env/                     在稳定路径重建
│   ├── dist/                           安装压缩包缓存
│   ├── logs/                           安装／环境验证记录
│   ├── activate.sh                     当前终端激活，不改全局 PATH
│   └── environment.json                本机工具版本及 SDK 标识
├── dell-voice-assistant/
│   └── src/assistant_runtime/plugins/ai_passport/firmware/
│       ├── main/ components/ tests/     自研固件唯一源码及测试
│       ├── tools/ docs/                打包工具及维护文档
│       ├── dependencies.lock           固定实际解析版本
│       └── build/                      可清理的构建缓存，不作发布目录
└── pp-firmware/                         本机交付与未来网站工作区
    ├── candidates/<app>/<version>/<app-sha256>/
    │                                   固定到真实镜像的候选；含私有调试包
    ├── releases/                       显式发布后的公开版本；当前为空
    └── site/                           未来网站源码；当前仅目录说明
```

固件源码继续在 PA 插件下，不另建源码副本；网站未来独立部署，不依赖 PA 模型授权或运行状态。
工具链不进入 PA wheel、生产 .venv 或网站资源。网站也不承载 ESP-IDF 在线编译。
SDK 遵循官方 `IDF_TOOLS_PATH` 机制，采用完整固定子模块及原生激活流程；更换目录后
重建 Python 环境和 CMake 缓存，不修改缓存中的绝对路径来伪装迁移完成。

[官方工具路径与安装说明](https://docs.espressif.com/projects/esp-idf/en/v5.5.3/esp32c3/get-started/linux-macos-setup.html)

## 未来发布网站的最小边界

第一步可以是静态目录、说明、历史版本、校验下载、浏览器安装及串口日志。
版本元数据由构建结果导出：应用标识、版本、真实构建标识、目标硬件、Flash 参数、
镜像偏移／大小／哈希、说明、验收状态及数据影响。当前 manifest 可复用，但还缺硬件
兼容和发布语义；不把现有私有调试包直接称作网站公开格式，本轮不提前改变 Schema。

发布操作选择一个已固定的候选，核对证据后生成独立公开清单与资源。不直接服务整个
`pp-firmware/`，不自动公开 ELF、MAP、sdkconfig、本机日志、Flash 读回文件或凭据。
公开版本文件保持不可变；推荐版本是可调整的目录指向，不能偷偷覆盖同版本镜像。
候选可由操作者自行刷写测试，显示“真机待验收”，不自动升级为正式推荐版。

先不引入多用户上传、自动审核、云端编译、OTA、下载统计或模拟器；若以后需要再单独设计。
发布入口已确认为本地文件／TOML 流程，可提供一个本地发布命令；不另做管理员上传后台。

## 下一阶段需要决定

1. 网站先供自己使用，还是对外开放；这决定管理员认证、上传与发布流程。
2. 首期是否只管理自研固件，是否还要接第三方／官网目录；不默认抓取或镜像他人固件。
3. 访问地址、HTTPS 证书与部署位置（无需强制域名）；LAN HTTP 可以展示目录，但不能承诺浏览器刷写可用。
4. 候选和正式版如何区分展示，以及发布前最低真机验收要求。

此文是初步方案，尚未以 Windows 真机验证网页刷写。库支持接口不等于已在本设备跑通。
