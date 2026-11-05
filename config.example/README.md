# 配置目录参考

新实例使用 `pa init <实例目录> --preset slim`（Linux 也可选 standard）生成配置及私有权限。此目录提供字段示例，**不要整体复制覆盖已有实例**；部署流程见[安装指南](../docs/guides/deployment.md)。

## 文件与入口

| 文件 | 内容 |
| --- | --- |
| `config.toml` | include 清单、实例 root、启用插件、工作目录及执行策略 |
| `voice.toml` | 助手名称、固定音频绑定及交互设置；语音当前停用 |
| `devices.toml` | 设备协议、地址和硬件参数 |
| `models.toml` | 主对话、模型别名、供应商和原生账号 |
| `ai-usage.toml` | AI 用量策略与限制 |
| `tasks.toml` | 任务并发、模型档位、工作文件和通知 |
| `memory.toml` | 记忆开关、提取模型与用途绑定 |
| `media.toml` | 媒体搜索、转存和下载 |
| `services.toml` | 网页、Apple Home、事件及第三方 MCP 设置 |

实际文件以初始化模板和入口 `include` 为准，不要求每个实例都包含全部示例片段。

## 组合与路径规则

只有入口允许 `include`，不递归扫描目录。片段必须位于入口目录内，不能通过 `..` 或软链接包含外部文件；最多 32 个文件，不得重复引用。单文件 TOML 也可直接加载。

同一表的不同字段可分散在多个片段；同一字段重复定义会报错，即使值相同。数组作为整体字段，不拼接，也没有后加载覆盖规则。

`root` 决定 `.env`、数据库、凭据、模型和业务数据的位置。相对 root 以入口目录解析，不依赖终端工作目录；`root=".."` 对应 `实例/config/config.toml` 布局。移动配置时保持其指向原数据目录，不改变插件路径的既有语义。

密钥存放在实例 `.env` 或专用私有凭据存储，普通 TOML 只引用变量名。配置及实例数据不提交 Git。

## 校验与生效

选定实例后执行：

```sh
pa doctor
pa models check
```

`doctor` 检查本机依赖与配置；模型检查不代表供应商实测通过。管理页“保存并生效”尝试在线更新，结构性变更提示显式重启。API 连接参数也由 PA 配置服务在安全边界切换，无需另行部署 PA 模型网关；自行维护的外部网关由其服务方负责部署。系统服务安装见[日常维护](../docs/guides/operations.md#安装系统服务)。

模型能力、思考档位、任务选型与输出限制由[模型模块](../src/assistant_runtime/core/models/README.md)和[任务模块](../src/assistant_runtime/core/tasks/README.md)维护。本页不另复制字段默认值。
