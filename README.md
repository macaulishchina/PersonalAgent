# Personal Agent

本地 Agent 框架，使用 Pi 作为唯一执行骨架，支持通过 API 或原生账号接入模型，提供网页对话和可持久化的后台任务。设备、媒体、搜索、事件、通知与记忆等能力由独立插件提供。

Linux 为现有运行平台。Windows 与 Windows Server 共用原生适配层，默认使用 slim。当前全部扩展插件（包括 web、voice_disabled）只认证 Linux，不能在 Windows 启用；standard 因包含这些扩展而被拒绝。Windows Server 2025 已有核心进程、IPC、Pi 和启动重启的本机验证；SCM、注销及系统重启仍待验收，Windows 11 与 Server 2022 尚未实机验证。完整流程见 [Linux／Windows 正式部署指南](docs/platforms.md)。

语音输入、语音对话和播报目前停用，待独立验收后启用；现有 USB 音频绑定及独立音频测试工具继续保留。

## 安装与配置

使用 Python 3.12。以下命令用于 Linux standard；Windows 使用部署指南中的 slim 流程，仍提供独立配置管理网页。不要在正在运行的生产环境中同步依赖。

```sh
uv sync --locked --extra standard --no-default-groups --no-editable
npm --prefix frontend ci
npm --prefix frontend run build
uv sync --locked --extra standard --no-default-groups --no-editable
scripts/setup_pi.sh /absolute/pi-runtime
.venv/bin/python -m assistant_runtime.app.cli init /absolute/instance
```

如果只需要本地执行底座，安装时使用 `--extra slim`，初始化时使用 `init /absolute/instance --preset slim`，无需构建聊天前端。

slim 和 standard 通过共享的 `core` 依赖组合包含全部核心插件代码及其运行依赖。两者的区别是默认启用哪些插件，而不是是否具备这些核心能力。它们使用同一套 CLI 和运行时，并默认启用 `integrations`，初始外部 MCP 注册表为空。未启用的插件不会加载；修改插件列表并重启即可启用。模型权重与外部服务不会自动安装或调用。

两种组合默认都提供独立配置网页，监听 `0.0.0.0:8047`，地址和端口可配置。slim 默认不启动聊天端口；本地执行 CLI 使用实例私有控制通道（Linux Unix socket、Windows 命名管道）。配置网页不依赖模型授权或业务插件启动成功，保存配置后需要明确应用并重启才能生效。详见[系统配置说明](src/assistant_runtime/core/configuration/README.md)。

将实例的 `pi_binary` 设置为安装脚本输出的绝对路径。standard 的最小网页入口可以在没有模型凭据、没有音频依赖的情况下启动，展示“待配置模型”状态；显式启用模型调用路径前，不允许发起推理。

部署组合详见[部署指南](docs/deployment.md)，插件扩展详见[插件开发指南](docs/plugin-development.md)。

### 私有配置与凭据

实例私有配置位于 Git 忽略的 `config/` 目录。入口 `config/config.toml` 显式引用模型、任务、设备、记忆和服务等配置片段。

`root` 指向包含 `.env`、`state/` 和 `models/` 的实例根目录，移动配置文件时应保持它不变。配置引用不允许递归，也不能静默覆盖同一字段。密钥只存放在私有 `.env` 或凭据存储中，不写入 TOML 或 Git。

### 模型接入

配置 `backend="pi"`、`pi_binary`、`main_model` 和 `main_reasoning_effort`。主对话与后台任务使用同一份 `model_api.entries` 模型别名目录。

OpenAI、Claude、Gemini 和 DeepSeek 官方预设提供默认地址、协议及密钥环境变量名，同时支持自定义端点和 OpenCode Go 预设。应如实声明图片、上下文和思考深度等能力，并在完成对应调用路径的验收后启用执行。任务档位 `fast / balanced / deep / ultra` 引用明确的模型别名和受支持的思考深度，不隐式继承父任务模型。Mem0 需要单独配置支持结构化输出的 `model_api.memory` 绑定。

相关说明：[模型配置](src/assistant_runtime/core/models/README.md)、[任务配置](src/assistant_runtime/core/tasks/README.md)、[环境变量示例](deploy/model-api.env.example)和 [Pi 适配器](src/assistant_runtime/core/pi/README.md)。

原生账号接入复用 Pi 的 OAuth／API Key 实现，不启动另一套 Agent。通过 `model_api.accounts` 定义具名账号，并将供应商绑定到相应账号。操作者可以使用 `model-accounts providers`、`login --account NAME`、`status --account NAME` 和 `catalog --account NAME` 管理私有连接。

ChatGPT 订阅使用官方 Sign in with ChatGPT 授权方式。该调用路径无法设置单次请求的输出上限，必须显式配置 `output_limit="model"`。支持登录、鉴权成功、静态模型目录中存在某个模型，以及实际能力通过验收，是不同的状态；启用前请阅读模型说明。

### 协议转换网关

需要协议转换的 API 调用路径使用固定版本、独立部署的 LiteLLM 网关；原生账号接入不强制要求网关。网关只负责协议转换，不管理工具或任务调度。

```sh
.venv/bin/python -m scripts.prepare_model_gateway --config config/config.toml
# 使用准备工具输出的私有 compose.json 路径，不要输出文件内容。
docker compose -f /absolute/instance/state/model-api/compose.json up -d
.venv/bin/python -m assistant_runtime.app.cli --config config/config.toml run
```

网关只监听本机回环地址，并使用私有凭据鉴权。模型切换、重试策略和已验收能力仍由应用配置管理。修改配置后，应先核对在途工作，再明确重启应用。

## 执行限制与故障恢复

AI 调用统一通过独立的 [ai_usage 插件](src/assistant_runtime/core/ai_usage/README.md)准入。一项工作及其关联任务、辅助模型、Jev 和付费搜索共享持久化额度。默认最多允许 **256 次上游请求**，第 257 次请求会在发送前被拦截；`warning_fraction` 默认设为 `0.5`，其他用量硬限制按需配置。

策略配置位于 `ai-usage.toml`。记录区分已确认结果、未知用量和估算费用。计量失败时暂停新的 AI 调用，不关闭设备或网页服务。

请求转发前先预留额度。继续执行、模型回退、升级和服务重启都保留原有工作额度。执行请求中断且结果未确认时，以未知状态阻止继续派发；仅缺少 token 统计，不会把已经确认的操作判为失败。

Pi 原生自动重试和付费缓存预热均停用。每次调用记录模型、时间、状态及上游返回的用量，不记录提示词正文或凭据；执行记录包含额度回执。

周期计划每次触发只派发一轮有限执行。任务模块负责有界的目标续跑，不通过让 Agent 一直等待来实现定时调度。额度耗尽表示执行已停止，不能当作成功完成，也不能以此自动换模型继续调用。

取消操作会清理排队输入、中断执行并核对工具是否结束。未确认结果的操作不得重放。模型升级要求执行处于空闲且已有可靠检查点。任务结果和通知保留各自的业务身份与反馈归属。

当前插件与 slim／standard 架构升级保留 Pi 对话、任务计划、通知、用量及操作证据。升级前先准备并校验配置候选，核对在途工作，停服备份，再替换代码、依赖和服务入口。不要使用历史执行状态重置工具完成本次升级，也不要自动重做未知操作。

## 插件与调用入口

网页文字／图片入口、CLI 和独立 MCP 服务复用同一套内核能力。当前执行拥有完整本地权限，操作系统沙箱仍为待实现能力。服务端绑定的 MCP 任务身份、资源范围、工具策略、版本检查和禁止重放规则继续生效。

当前应用的 Pi 适配器尚未实现交互式 MCP OAuth 登录；应显式配置凭据并验证连接。

- [任务](src/assistant_runtime/core/tasks/README.md)：计划调度、事件收件箱、目标进展、模型选择、执行过程和结果归属。
- [通知](src/assistant_runtime/core/notifications/README.md)：持久化通知收件箱及聚合展示。
- [事件](src/assistant_runtime/core/events/README.md)：由事件源报告观察结果；保存事件本身不会触发推理。
- [媒体](src/assistant_runtime/plugins/media/README.md)：资源搜索、网盘转存、本地下载和文件管理。
- [网页搜索](src/assistant_runtime/plugins/search/README.md)：通过独立配置的供应商搜索或读取公开网页。
- [外部 MCP 接入](src/assistant_runtime/core/integrations/README.md)：带版本的 stdio／HTTP MCP 定义及限定范围的工具策略。
- [PipeWire](src/assistant_runtime/plugins/pipewire/README.md)：固定音频资源和独立音频输入测试。
- [摄像头网关](deploy/go2rtc.md)：稳定的摄像头身份和显式设备验收。

设备观察、事件写入和记忆查询本身都不会独立激活模型。媒体采集需要明确请求，默认不保留原始音频或视频。模型工具不能切换固定音频绑定。

### 技能与私有注册表

助手技能分为业务插件随包交付的通用操作技能，以及私有 `config/skills/<名称>/SKILL.md` 中的实例设备经验。后者需在配置总入口用 `instance_skills = ["名称"]` 显式选择，默认不加载。

开发工具的 `.agents/` 和 `.agent/` 完全忽略，不作为助手输入。根目录 `AGENTS.md` 是纳入版本管理的开发规范。

MCP 和事件源注册表分别位于私有 `config/mcp/servers.json`、`config/observations/sources.json`。模板只用于新实例，不要覆盖已有注册表。

## 开发与验证

```sh
.venv/bin/python scripts/check.py --group execution --group tasks --group models
.venv/bin/python scripts/check.py
.venv/bin/python -m scripts.verify_integrations
```

开发过程中可以运行针对性检查。最终源码或依赖修改后，提交或部署前需要通过全量检查。

原生 Pi 测试使用私有合成 Responses 端点，运行真实子进程、MCP 调用和文件操作，覆盖混合模型委派、用户澄清、模型选择、取消、重启、失控循环防护及上下文压缩预算，不发起付费请求。真实供应商和硬件需要单独明确验收。`scripts.verify_models` 强制限制合成测试数据的请求与输出规模，不要为方便测试而移除这些限制。

`scripts/check_audio_input.py` 保留为独立音频测试工具；声学测试前应先停止助手服务。语音支持和操作系统沙箱属于后续工作，不应以模拟成功代替实际实现。

私有回执和实验放在 Git 忽略的 `state/acceptance/` 中，仅在有明确要求时生成报告。

模块职责见[架构说明](ARCHITECTURE.md)，工程规则见 [AGENTS.md](AGENTS.md)。生产准备与切换见 [Pi 发布操作指南](deploy/pi-release.md)。`scripts.check_pi_release` 只做配置预检查，不代替真实供应商验收或生产部署。
