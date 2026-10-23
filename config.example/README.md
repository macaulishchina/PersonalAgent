# 配置目录

首次安装将整个 `config.example/` 复制为实例的 `config/`。修改 `config/`，不要
修改模板。入口为 `config/config.toml`，私有配置不进入 Git。

| 文件 | 内容 |
| --- | --- |
| `config.toml` | include 文件清单、实例 root、运行模式、插件、工作目录与执行策略 |
| `voice.toml` | 助手名称、唤醒、交互、主音频绑定、静默时段 |
| `devices.toml` | 各设备协议、地址和硬件参数 |
| `models.toml` | 主会话订阅模型、API 供应商/模型、Jev |
| `tasks.toml` | 后台并发、fast/balanced/deep/ultra、工作文件和通知 |
| `memory.toml` | 记忆开关、提取模型和可选 model_api.memory |
| `media.toml` | 影视搜索、转存、下载 |
| `services.toml` | Web、Apple Home、事件服务、第三方 MCP 框架设置 |

入口明确列出文件，不扫描目录；只有入口允许 `include`。文件必须位于入口目录
内，不能通过 `..` 或软链接包含外部文件。最多 32 个文件，不能重复引用。
单文件 TOML 仍可直接加载，不要求所有测试实例都拆分。

相同表的不同字段可以分布在不同文件，例如 `models.toml` 的
`model_api.enabled` 和 `memory.toml` 的 `model_api.memory`。同一字段重复定义
会报错，即使值相同也不允许；数组整体视作一个字段，不拼接。没有加载顺序覆盖规则。
错误会标明文件/字段，不输出配置值。缺失文件或错误配置不会静默使用默认值启动。

`root` 决定 `.env`、数据库、凭据、模型和运行数据所在实例目录。相对 `root`
以入口所在目录解析，不依赖启动命令的工作目录。模板 `root=".."` 适合
`实例/config/config.toml` 布局；迁移已有实例时建议填写原来的绝对 root。
其他插件路径保持各自现有语义，不随它们所在的片段文件改变。

```sh
.venv/bin/python -m assistant_runtime.app.cli --config config/config.toml doctor
.venv/bin/python -m assistant_runtime.app.cli --config config/config.toml models check
.venv/bin/python scripts/install_service.py --config "$PWD/config/config.toml"
```

`doctor` 检查本机依赖和配置，`models check` 校验模型配置；安装脚本更新并启用
自启单元，不启动或重启服务。实际配置变更仍需显式重启。API 供应商变化还需重新生成网关配置。
固定主音频绑定、正在执行的任务应按原部署规则处理，不通过迁移配置强制中断。

API 的 `model_api.enabled` 控制总开关，供应商的 `enabled` 控制单独接入。
密钥放实例根目录 `.env`，模型配置只引用变量名。供应商开关不是数据分类过滤器。

每个 API 模型必须用 `reasoning_efforts` 声明有效档位，`default_reasoning_effort` 指定默认值。
辅助推理和后台任务可明确选择其中一档；未指定的任务选型沿用 Jev 与原策略。
`model_api.max_output_tokens` 控制辅助推理预算，`execution_max_output_tokens` 控制后台执行预算；
两者都包含思考 token，模型条目可以单独覆盖。
