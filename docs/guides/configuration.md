# 配置模型与访问

前提：已完成[安装与首次启动](deployment.md)，配置网页正在运行。

## 登录配置网页

默认地址为 `http://127.0.0.1:8047`。口令读取命令：

```text
pa configuration token --management <实例目录>/management.toml
```

忘记口令时将 `token` 换成 `reset-token`，在服务器本机重置。路径含空格时加引号。

| 入口 | 默认监听 | 地址配置 | 口令 |
| --- | --- | --- | --- |
| 系统配置网页 | `0.0.0.0:8047` | `management.toml` 的 `host`、`port` | 独立管理员口令 |
| standard 聊天网页 | `127.0.0.1:8046` | `config/services.toml` 的 `[web]` | 实例的 `web-access-token` |

远程访问使用服务器 IP，并按需放行防火墙。局域网聊天需要将 `[web].host` 改为 `"0.0.0.0"`；公网访问需另行配置 HTTPS 反向代理及受信来源。

## 配置一个可用模型

在“模型与账号”依次完成：

1. **账号与供应商**：选择原生账号的 OAuth／API Key 方式，完成授权或录入私有凭据，供应商引用该账号。原生账号路线不需要 LiteLLM 或 Docker。
2. **模型**：填写稳定别名、供应商、实际模型 ID、可用思考档位和用途能力。
3. **用途绑定**：将 `main_model` 设为该模型别名，`main_reasoning_effort` 选用其支持的档位。任务和记忆仅在启用时配置各自绑定。
4. **启用**：开启所需供应商和 `model_api.enabled`，保存并应用。
5. **确认**：进入 `pa` 发送消息，检查回答及执行结果。登录成功或模型目录可见不等于调用已经验收。

ChatGPT 原生账号使用独立 OAuth 授权，并显式设置 `output_limit="model"`。账号、模型能力和具体字段见[模型模块](../../src/assistant_runtime/core/models/README.md)。

也可在选定实例后使用 CLI 管理已声明的账号：

```sh
pa model-accounts providers
pa model-accounts login --account NAME
pa model-accounts status --account NAME
pa model-accounts catalog --account NAME
pa models check
```

跨机器 OAuth 登录时按终端提示转发回调端口。密钥使用页面私有凭据入口或实例 `.env`，不写入普通 TOML；不复制其他应用会轮换的登录令牌。

## 需要协议转换时

使用 `preset` 或自定义 URL 的 API 路线若需要协议转换，按[模型模块](../../src/assistant_runtime/core/models/README.md)部署固定版本 LiteLLM 网关。在应用目录，用当前 `.venv` 的 Python 执行：

```sh
python -m scripts.prepare_model_gateway --config /absolute/instance/config/config.toml
docker compose -f /absolute/instance/state/model-api/compose.json up -d
```

使用准备工具实际输出的私有路径。网关只监听回环地址；页面“应用”不会更新外部容器，需完成页面列出的网关部署步骤。

## 保存、应用与插件

“保存”写入配置文件；“应用”才重启业务并加载新配置，可选择等待工作结束或确认终止。配置网页在业务重启期间保持可用。

插件按显式列表启用，修改后需应用。Windows／Server 只能启用已认证 Windows 的插件；已有不支持配置可在管理页取消插件并修正依赖，系统不会自动删除它们。

普通配置、静态凭据和已选技能纳入配置版本；OAuth 登录／退出是独立账号操作，不随配置回滚。管理事务和恢复细节见[配置模块](../../src/assistant_runtime/core/configuration/README.md)。

## 配置文件在哪里

实例目录保存 `management.toml`、`config/`、`.env` 和 `state/`。其中 `config/config.toml` 显式引用片段；`root` 决定数据位置，移动配置时保持其指向不变。

- [配置目录参考](../../config.example/README.md)：片段、路径和字段规则。
- [模块索引](../reference/modules.md)：MCP、技能、任务、用量及扩展设置。
- [日常维护](operations.md)：备份、升级和修复失败配置。
