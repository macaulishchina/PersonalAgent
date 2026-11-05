# 开发与验证

先阅读 [AGENTS.md](../../AGENTS.md) 和[架构](architecture.md)，在当前项目 checkout 和同一个 `.venv` 中开发。测试使用独立临时实例，不修改运行实例。

## 开发环境

安装脚本固定使用 `--no-editable`；给脚本加 `--extra dev` 只会安装开发工具，不会切换为 editable。需要源码开发环境时，在仓库根目录直接执行下面的命令，使用 uv 默认的 editable 安装，并保留所需依赖组：

```sh
uv sync --locked --python 3.12 --extra slim --extra dev --no-default-groups --inexact
```

需要聊天网页时将 `--extra slim` 换成 `--extra standard`，前端资源的构建方式见下表。不要向正在运行的生产环境同步依赖。

使用安装脚本维护开发环境时显式加 `--extra dev`，后续会记住选择；检查安装流程用 `python scripts/check.py --test test_install_script`，预览用 `python scripts/install.py --dry-run`。脚本预览不执行前端构建或同步应用环境。

以下命令在仓库根目录运行。`python` 必须来自项目 `.venv`：Windows 可用 `.\.venv\Scripts\python.exe`，Linux 可用 `.venv/bin/python`。

## 源码修改何时生效

可编辑安装（editable）把 Python 包指向源码目录；正式安装使用已安装的程序副本。当前安装脚本固定使用 `--no-editable`，用于正式安装，不是自动重载开发服务器。

| 修改内容 | editable 安装下的生效方式 |
| --- | --- |
| 配置网页的 `core/configuration/static/` JS／CSS／HTML | 刷新网页；管理服务按请求读取文件 |
| Python 后端代码 | 重启对应进程；配置管理接口代码变化需要重启管理进程 |
| 对话网页 `frontend/` 源码 | 先执行 `npm --prefix frontend run build`，再刷新网页 |
| 依赖、命令入口、打包元数据 | 重新安装／同步环境，必要时重启 |
| 实例配置 | 走“保存并生效”或显式应用流程，与代码重载无关 |

非 editable 安装还需要先更新安装包。`pa restart` 重启所选实例的管理和业务进程，不会执行安装或前端构建。开发与验收使用独立实例，不能为验证热更新而重启生产服务。

## 自动检查

```sh
python scripts/check.py --list
python scripts/check.py --group execution --group tasks --group models
python scripts/check.py --test test_integrations.AdapterTest
python scripts/check.py --static-only
python scripts/check.py
```

按影响范围选择验证，不把每次提交、推送或 editable 开发实例更新都当作正式发布。

| 改动范围 | 默认验证 |
| --- | --- |
| 文档、提示文字 | 内容、链接、命令及必要的页面核对 |
| 局部界面交互 | 相关静态检查、对应页面操作验证；涉及后端时补直接调用链测试 |
| 局部后端功能 | 相关静态检查、所属模块及直接调用方测试，覆盖实际改变的错误和生命周期边界 |
| 依赖、打包、安装行为 | 上述检查，加受影响的安装包验证 |
| 正式发布、明确要求全量验证，或影响无法可靠限定的广泛共享契约变更 | 全量检查；正式安装版发布再做安装包验收 |

例如给模型列表增加真实推理测试，应重点验证按钮操作、鉴权、当前草稿、协议调用、用量限制和请求取消；不因此自动执行摄像头、语音、媒体等无关回归。配置网页使用 `core/configuration/static/` 静态文件，其修改不要求构建独立的对话网页 `frontend/`。

已通过的检查在相关输入未变时继续有效。源码和安装包的同一套验收不默认重复；需要扩大范围时先说明新增风险与验证目的。针对性检查必须如实标注，不能称为全量通过。

开始小需求时先明确完成边界并复用现有实现。普通改动超过约十分钟时复查是否扩大了需求、重复验证或在等待无关流程，及时说明实际阻断；这不是省略相关验证或提前结束工作的理由。

测试按模块进程并发，默认最多 3 个；共享资源或诊断问题时使用 `--jobs 1`。计时敏感失败先隔离到失败模块复查，不默认把整套测试改成串行重跑。无关失败须记录证据并单独跟踪；新增或尚未解释的相关失败仍阻断交付。真实设备、供应商和已安装服务验收单独执行。

## 版本与兼容性检查

修改版本契约、插件声明或装配路径时，先运行：

```sh
python scripts/check.py --test test_versions --test test_plugin_framework --test test_plugin_certification --test test_plugin_backends
python scripts/check_release.py
```

`check.py` 的静态阶段已包含发布声明检查，`--static-only` 也会运行它；单独执行 `check_release.py` 可快速定位声明问题。它核对内核版本来源、内置插件及示例包声明、依赖范围和旧平台文件残留，不导入设备实现。

框架 wheel 构建后运行 `python scripts/check_release.py --wheel <框架wheel>`，检查实际包中的版本与声明是否和源码一致。针对真实实例配置的候选预检使用 `pa --config <测试配置> upgrade-check <框架wheel> --plugin-wheel <独立插件wheel>`；只有框架更新时省略 `--plugin-wheel`。这两个命令分别解决包与源码一致性、候选与实例声明兼容性，不替代安装版运行验证。

插件作者至少覆盖：正常兼容、缺失／无效声明、内核／API／依赖版本不匹配、静态与运行时声明不一致、禁用插件可保留并移除。检查拒绝发生在实例创建和设备打开之前；候选包预检不得执行候选代码。启动记录还须覆盖失败不覆盖上次成功、关闭后再启用旧插件仍拒绝降级，以及两次真实启动。代码更新需重启，不把配置热更新测试当作代码升级验证。

## 原生平台与安装包

插件规范回归使用 `python scripts/check.py --test test_plugin_standards --test test_tool_outcomes`。
前者逐个核对全部内置插件的声明、能力消费／提供、资源前提、配置和技能；后者枚举
第一方 MCP 工具的 Schema、开放字段说明、属性和示例，并验证无副作用的参数拒绝、
运行兼容、直接调用／MCP 一致性。规范检查不启动真实设备或供应商请求；插件自己的
业务回归、真实 Pi 合成链路和原生平台验收仍分别运行。

使用已安装好的 Python 和 Pi，在应用目录按需运行：

```sh
python scripts/verify_native_deployment.py
python scripts/verify_native_deployment.py --installed
python scripts/verify_native_chat.py
python scripts/verify_native_chat.py --installed
python scripts/verify_configuration_console.py --browser <Chromium或Edge可执行文件>
python scripts/verify_configuration_console.py --installed --python <安装环境的Python> --browser <Chromium或Edge可执行文件>
```

源码与安装包分别记录结果。脚本使用独立临时实例；`verify_native_deployment.py` 在 Linux 和 Windows 默认都检查 slim／standard，可用 `--preset slim` 缩小范围；这与 Windows 的 `pa init` 默认 slim 是两回事。standard 根据实际平台认证验收（现有内置扩展均已支持）。正常退出或捕获异常时清理测试目录；强制终止留下的目录按日志路径核对后处理。

真实 Pi 子进程和本地合成模型可以验证工具、上下文、澄清、取消及恢复，不代表真实供应商、物理设备或系统服务通过。不要为获得绿色结果删除用量限制或隐藏缺失依赖。

配置网页验收由开发环境的 Playwright 驱动浏览器；`--python` 仅选择被验证的服务环境，`--installed` 禁止用源码覆盖安装包。脚本检查保存生效、显式重启、插件认证、内核／扩展版本展示、配置修订标识、嵌套表单、密码确认和坏配置修复。原生部署脚本还核对每次启动的成功版本记录。配置事务的并发准入、连接替换、失败回滚和监听切换失败由 `test_configuration_service` 覆盖；执行中模型切换与多轮上下文由 `test_conversation_native` 使用真实 Pi 和合成模型验证。

## 如何记录结果

分别列出：

- **通过**：实际运行且满足断言或验收条件。
- **跳过**：测试明确跳过，注明平台、权限或能力条件。
- **依赖错误**：缺失可选依赖导致的导入／执行错误，不能计为通过。
- **新增失败**：相对于已知基线新增的失败或错误。
- **未验证**：没有实际执行的供应商、设备、OS 或服务生命周期。

历史数据见[平台验收记录](../archive/platform-acceptance-2026-10.md)。2026-10-24 的记录有 12 项既有依赖错误；2026-10-25 的 Windows 扩展适配使用独立扩展依赖目录完成了新的全量检查，结果另记在同一文件中，不改写历史结果。

私有回执和实验留在忽略的实例存储或临时目录，只在明确要求时生成报告。发布验证见[发布与切换](release.md)。

## Windows 扩展专项

在 Windows／Server 的开发环境安装所需 extras：`windows-devices`、`hid-buttons`、`airplay`、`apple-home`、`camera`、`keyword`，并保留其他已选 extras。不要向正在运行实例使用的环境同步测试依赖。本轮在同一开发 Python 旁使用独立临时依赖目录，不修改运行安装环境。

```powershell
.venv\Scripts\python.exe scripts/check.py --test test_windows_extensions --test test_hid_buttons --test test_plugin_backends
.venv\Scripts\python.exe scripts/verify_native_deployment.py --preset slim --plugin devices --plugin pipewire --plugin v4l2 --plugin airplay --plugin gesture --windows-device-fixtures
.venv\Scripts\python.exe scripts/verify_native_deployment.py --preset slim --plugin events --plugin observations --plugin notifications --plugin web
```

`test_windows_extensions.NativeWindows` 只在 Windows 执行真实 HIDAPI／WASAPI／DirectShow 枚举、缺失设备拒绝、NTFS 安全文件操作、私有 ACL、FFmpeg 编码和进程清理；其余合成传输断言不等于物理设备验收。不自动录音、播放或操作设备，不连接真实供应商。实际唤醒推理需提供既有 FSMN V2 模型包；缺少资产只能记录为未验收。

额外插件的完整进程验收会检查插件 ready、管理端口鉴权、异常所有者退出后的回收和重启；每个实例使用独立临时目录及端口。`--installed` 在安装包环境重复同样的命令。预设能否初始化由实际平台认证决定，Windows 默认仍为 slim。
