# 模块索引

用户操作先看[使用指南](../README.md)。以下 README 由各模块维护，记录配置、接口和专项验收；部分还会随安装包交付，因此保留在代码旁，避免在 docs 中复制第二份。

模块中的 `pa` 示例要求已按[安装指南](../guides/deployment.md)设置 PATH；Python 验收脚本在仓库根目录使用同一个 `.venv` 执行。

## 核心能力

| 模块 | 阅读内容 |
| --- | --- |
| [configuration](../../src/assistant_runtime/core/configuration/README.md) | 配置网页、鉴权、事务、应用与修复 |
| [cli](../../src/assistant_runtime/core/cli/README.md) | CLI 入口、实例选择和后台启动契约 |
| [conversation](../../src/assistant_runtime/core/conversation/README.md) | 会话、队列、迁移和结果归属 |
| [execution](../../src/assistant_runtime/core/execution/README.md) | 单次执行、附件、澄清和取消 |
| [pi](../../src/assistant_runtime/core/pi/README.md) | 唯一执行器、上下文与模型适配 |
| [models](../../src/assistant_runtime/core/models/README.md) | 模型、原生账号、API 与网关 |
| [ai_usage](../../src/assistant_runtime/core/ai_usage/README.md) | 额度、准入、计量和限制 |
| [workfiles](../../src/assistant_runtime/core/workfiles/README.md) | 工作文件、引用与交付 |
| [integrations](../../src/assistant_runtime/core/integrations/README.md) | 外部 MCP 注册、验证和工具范围 |
| [tasks](../../src/assistant_runtime/core/tasks/README.md) | 任务、计划、续跑和结果投递 |
| [notifications](../../src/assistant_runtime/core/notifications/README.md) | 通知收件箱与聚合 |
| [events](../../src/assistant_runtime/core/events/README.md) | 事件源、观察记录和订阅 |
| [个人助手交互](../../src/assistant_runtime/core/speech/interaction/README.md) | 交互状态机；语音当前停用 |

核心身份不代表已启用或其所有依赖都适用于当前平台。是否可以启用，以插件声明和依赖校验为准。

## 扩展插件

当前全部扩展仅认证 Linux，Windows／Server 不允许启用；语音停用。平台声明和启用规则见[插件开发](../development/plugins.md#平台基础设施)。

| 模块 | 阅读内容 |
| --- | --- |
| [media](../../src/assistant_runtime/plugins/media/README.md) | 媒体搜索、转存、下载和文件管理 |
| [search](../../src/assistant_runtime/plugins/search/README.md) | 公开网页搜索与读取 |
| [observations](../../src/assistant_runtime/plugins/observations/README.md) | 设备观察与事件来源 |
| [decisions](../../src/assistant_runtime/plugins/decisions/README.md) | 事件决策与触发规则 |
| [go2rtc](../../src/assistant_runtime/plugins/go2rtc/README.md) | 已注册摄像头流的适配 |
| [v4l2](../../src/assistant_runtime/plugins/v4l2/README.md) | Linux 本地视频输入 |
| [pipewire](../../src/assistant_runtime/plugins/pipewire/README.md) | Linux 固定音频资源 |
| [speech](../../src/assistant_runtime/plugins/speech/README.md) | 语音适配与当前停用边界 |
| [airplay](../../src/assistant_runtime/plugins/airplay/README.md) | AirPlay 设备适配 |
| [apple_home](../../src/assistant_runtime/plugins/apple_home/README.md) | Apple Home 接入 |
| [adb_tv](../../src/assistant_runtime/plugins/adb_tv/README.md) | ADB 电视操作 |
| [reyee](../../src/assistant_runtime/plugins/reyee/README.md) | 路由器设备接入 |
| [hid_buttons](../../src/assistant_runtime/plugins/hid_buttons/README.md) | HID 按钮输入 |

聊天网页和语音占位的使用边界见[安装组合](installation.md#slim-与-standard)。实际插件清单及认证使用 `pa plugins` 查询，本文不另维护版本表。

## 配置与示例

- [配置目录](../../config.example/README.md)：片段、root、凭据与校验。
- [实例技能](../../config.example/skills/README.md)：私有技能的显式选择。
- [独立插件示例](../../examples/sample-plugin/README.md)：安装、配置和公共接口。
- [摄像头网关部署](../guides/cameras.md)：Linux 硬件接入步骤。

运行时技能的 `SKILL.md` 保留在所属模块，由插件声明选择，不纳入用户部署步骤。开发工具技能不作为运行时输入。
