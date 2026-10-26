# Personal Agent 文档

第一次使用按顺序阅读前四项；已有实例从日常维护开始。

## 开始使用

| 顺序 | 文档 | 解决的问题 |
| --- | --- | --- |
| 1 | [安装与首次启动](guides/deployment.md) | Windows／Server 或 Linux 上安装、创建实例 |
| 2 | [配置模型与访问](guides/configuration.md) | 登录配置页、连接模型、保存并应用 |
| 3 | [CLI 使用](guides/cli.md) | `pa` 聊天、会话、队列、补充和取消 |
| 4 | [日常维护](guides/operations.md) | 后台与系统服务、日志、备份、升级、排障 |

Windows 和 Windows Server 默认使用 slim；Linux 默认 standard。两类平台均可按插件认证选择 slim 或 standard。两种组合都有 CLI 和独立配置页，聊天网页仅 standard 默认提供。语音当前停用。

## 按需配置

- [安装选项](reference/installation.md)：slim／standard、目录、自定义 Python／Pi 位置、wheel 安装。
- [模块索引](reference/modules.md)：模型、任务、MCP、用量、设备及扩展插件的详细说明。
- [摄像头网关](guides/cameras.md)：Linux 上部署 go2rtc、接入和检查摄像头。
- [配置文件参考](../config.example/README.md)：配置片段、路径和凭据规则。

## 开发与维护

1. [架构](development/architecture.md)：进程、模块边界、执行和数据归属。
2. [插件开发](development/plugins.md)：公共接口、版本／平台声明、配置描述与生命周期。
3. [开发与验证](development/testing.md)：环境、针对性测试、全量检查和实机验收。
4. [发布与切换](development/release.md)：版本递增规则、候选包检查、发布切换和数据回退。
5. [受控执行接入](development/controlled-execution.md)：可信授权、多轮上下文、Windows 隔离、停止核对和不可变产物。
6. [文档维护](development/documentation.md)：文档放置规则与修改检查。

工程约束以根目录 [AGENTS.md](../AGENTS.md) 为准。模块 README 保留在所属代码旁，由[模块索引](reference/modules.md)统一导航。

## 历史记录

[归档目录](archive/README.md)保存需求来源、设计计划和有日期的验收证据。归档中的旧命令、旧执行器及测试结论不作为当前操作步骤。
