# Personal Agent

在自己的机器上运行的 Agent。通过 `pa` 持续聊天，使用 Pi 执行任务，保存会话与结果；模型、任务、MCP 和设备能力按需配置。

## 从这里开始

- **第一次安装**：[安装与首次启动](docs/guides/deployment.md) → [配置模型](docs/guides/configuration.md)。
- **已经有实例**：[CLI 使用](docs/guides/cli.md) → [日常维护](docs/guides/operations.md)。
- **开发或扩展**：[架构](docs/development/architecture.md) → [插件开发](docs/development/plugins.md)。
- **完整导航**：[文档目录](docs/README.md)。

## 选择平台

| 平台 | 安装组合 | 默认入口 |
| --- | --- | --- |
| Windows / Windows Server | slim | CLI、独立配置网页 |
| Linux | standard；也可选 slim | CLI、独立配置网页；standard 另有聊天网页 |

当前扩展插件仅认证 Linux，Windows 禁止启用，包括 `web`、`voice_disabled`。语音当前停用。Windows Server 2025 已完成核心启动、对话和重启验证；系统服务、注销及整机重启仍待实机验收，详见[验收记录](docs/archive/platform-acceptance-2026-10.md)。

## 已安装后的日常入口

```text
pa use <已有实例目录>
pa
```

实例目录存放配置、凭据和会话数据；`pa use` 只记住选择。以后运行 `pa` 即可聊天，未运行的实例会自动启动。退出聊天不停止后台；查看、停止和重启使用 `pa status`、`pa stop`、`pa restart`。

命令入口为 `pa`，需要将安装环境的可执行目录加入 PATH，设置方法见[安装指南](docs/guides/deployment.md)。

贡献代码前阅读 [AGENTS.md](AGENTS.md)；修改文档参照[文档维护约定](docs/development/documentation.md)。
