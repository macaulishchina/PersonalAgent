# 公开模型路由与可靠任务接入

适用版本：PA 0.4.14 起；公共插件 API 2、实例数据格式 1。
这是可信插件公共能力，不是多租户隔离或操作系统沙箱。

## 读取已生效目录

插件声明对 models 的依赖及 `ModelRequest` 能力，通过 `context.invoke` 调用：

```python
from assistant_runtime.api.models import ModelRequest
from assistant_runtime.api.contracts import TaskRequest
from assistant_runtime.api import ConversationRequest

catalog = await context.invoke(ModelRequest("catalog"))
row = next(row for row in catalog["models"] if row["model"] == "registered-alias")
condition = {key: row["route"][key] for key in ("model", "revision")}
```

`models` 保留登记别名、可用性、支持/默认深度，并提供：

- `route = {model, provider_id, upstream_model, revision}`：配置路由的公开投影。
- `display_name/description/provider_display_name/provider_description/avatar`：公开外观元数据。
- `unavailable_reason`：不可用的安全原因码；可用时为 null。
- 顶层 `default = {route, reasoning_effort, effort_source}`：实例主会话默认，深度来源为 `main_conversation`。

接口不访问外部目录、不测试密钥、不推理，不返回端点、凭据或环境变量名。
不同登记连接可区分；协议、端点、原生账号身份或上游型号变化会改变 revision。
展示字段、凭据轮换和默认深度变化不改变路由 revision。账号是登记身份，不通过密钥推断真实账户。
目录是选择依据；可用仅表示配置启用，不能证明网关实际型号、能力或后续推理成功。
保留 `TaskRequest("models")` 的原结构；其中 models 行补充同源路由/外观数据，configuration/model_policy 仍由 tasks 拥有。
调用方组合目录和任务策略生成自己的菜单；PA 不保存调用方的 AI 成员或业务身份。

## 严格任务与模型默认深度

```python
args = {
    "request_id": stable_uuid,
    "spec": {
        "title": "Authorized analysis",
        "prompt": "Bounded authorized objective",
        "execution": {
            "model": condition["model"],
            "expected_model": condition,
            "effort_mode": "model_default",
        },
        "tools": allowed_tools,
        "notify_completion": False,
        "notify_voice": False,
        "auto_archive": False,
    },
}
task = await context.invoke(TaskRequest("create", args))
```

`expected_model` 是可选的严格约束，model 与其必须一致。接受及派发核验路由、启用状态与实例固定策略。
约束随任务/轮次快照持久化；删除、停用、改绑或策略冲突拒绝新执行，不重新选型，不允许模型升级交接绕过约束。
继续和显式重试仍受约束；未知操作继续使用已有核对机制。历史、结果、回执读取不依赖当前模型可用。

`effort_mode="model_default"` 显式使用模型条目有效默认值，接受后冻结，包括供应商控制的 `auto`。
它不能与显式 `reasoning_effort` 同时提交，不触发额外的 active/shadow 模型决策。实例固定策略冲突直接拒绝。
省略该字段保留原深度解析和具体档位要求；不要用新语义重新解释旧调用的省略参数或 auto。
主 AI 自主选择具体深度时，使用既有 reasoning_effort/model_source/effort_source/reason，按需加 expected_model。
模型选择和深度来源分别记录。主会话继续使用 main_reasoning_effort，不改成模型条目默认值。

## 会话预期条件与执行关联

```python
await context.invoke(ConversationRequest("submit", {
    "session_id": session_id, "request_id": stable_uuid, "text": text,
    "expected_model": condition,
}))
receipt = await context.invoke(ConversationRequest("receipt", {
    "session_id": session_id, "request_id": stable_uuid, "include_execution": True,
}))
```

可选条件仅约束本次请求，不创建固定模型会话。条件进入原始提交指纹，排队编辑不能修改它。
相同请求的丢回执重试先命中原记录，不因后来配置变化重新解析或重跑。
接受与派发检查，执行容量等待后仍校验；不传条件的旧调用行为保持。
`include_execution` 默认为 false，旧 receipt/receipts 返回结构保持。
启用后增加 execution：requested、route、reasoning_effort、execution_id、turn_id、accepted_at/started_at/finished_at、dispatch_state，准备拒绝时有 failure。
排队或历史没有证据的字段缺省/未知；不得用当前目录补造历史。
任务 get 的 run.data.execution_facts 保存实际采用的公开 route、深度和开始时间；其他时间、状态、结果引用及错误沿用原字段。
Pi 原生完成记录同时保留路由事实；这证明宿主选择，不证明供应商网关背后的真实底层版本。

## 创建回执与只读恢复

```python
receipt = await context.invoke(TaskRequest("receipt", {"request_id": stable_uuid}))
# {found: True, request_id, task_id} 或 {found: False, request_id}
checked = await context.invoke(TaskRequest("receipt", args))
# 提供原 spec 和 feedback_owner 时额外返回 matches；不返回原请求正文。
```

查询不创建、不派发、不重新选型。记录通过现有 SQLite 事务及 request_id 唯一键持久化。
UUID 沿用原创建形式；原有全局唯一及内容冲突约束不放宽。后台模型不能调用此宿主查询维护入口。
单独 request_id 只查询关联；需要内容一致性比较时必须提供原始 spec（不是解析后的 spec）。
查询失败抛出错误，不能解释成 found=false；暂时未找到不保证原请求之后不会到达。
保持原身份核对，未知不自动重放。原工具上下文结束后，不能把一次委派重新创建成独立额度任务。

## 外观与头像

```toml
[model_api.providers.team.presentation]
display_name = "团队连接"
description = "公开说明"
avatar = "builtin:generic"

[model_api.entries.example.presentation]
display_name = "公开模型名称"
```

名称 1～80 字符、说明最多 400 字符。模型继承连接头像，默认 generic。
内置 generic/openai/anthropic/google/deepseek 为受控字母图标，不根据兼容协议猜测品牌。
配置页提供编辑、预览、上传、恢复默认。上传 PNG/JPEG/WebP 最大 2 MiB、1600 万像素；服务端仅取首帧、清除元数据，输出不超过 256×256/256 KiB 的 PNG。

管理端 POST /api/model-avatar 使用管理员会话、同源校验、JSON {version,data_base64}，返回 avatar 引用和配置资源操作。
上传不落盘、不应用；引用与资源必须在同一配置保存事务提交。取消不产生图片文件，保存失败回滚配置和资源。
管理端 GET /api/model-avatar/ID?version=ID 只读保存配置中的资源，返回私有缓存和 ETag。
运行侧 `ModelRequest("avatar_read", {"asset_id": ID, "version": ID})` 仅允许已生效目录正在引用的资源，返回规范化 PNG 的 media_type/sha256/data_base64。
ID 与版本均为规范化内容摘要，内容不可同版本覆盖。插件自行同源代理和缓存，浏览器无需 PA 管理 Cookie。
未引用文件沿用配置恢复所需的保留方式，不提供任意路径、远程图片或可执行 HTML/SVG 读取。历史外观快照由接入方保存。

## 错误、归属和容量

新路由/默认参数拒绝使用公开 `CapabilityRequestError`（继承现有 ToolFailure/ValueError），可读 code 与 dispatch_state=not_dispatched，并复用现有工具传输的结构化未派发结果。
常见 code：invalid_arguments、model_unavailable、model_binding_changed、model_policy_conflict、reasoning_effort_unsupported。
既有 ToolFailure、ModelCallError 和传输异常保持原契约；普通异常、超时不能证明没有派发。
任务初始错误、dispatch、执行历史和会话回执保存安全错误信息，未知仍不自动重跑。

可信工具上下文创建任务继续复用来源、工作额度、通知开关及结果引用。客户端不能传入预算身份。
工具白名单应覆盖全部任务创建入口，才可限制某类后台任务不得再委派；PA 不全局禁用已有子任务。
通过 tasks.concurrency 小于 execution.concurrency 可为其他执行留容量，但这不是全局前台优先或抢占保证；总并发 1 不能同时保留一个前台席位并运行后台任务。
提示缓存不预热，不承诺跨任务命中；遥测缺失保持未知。

## 查询与验收

继续使用 task list 的分页/known、get、execution_info 和原结果读取接口，没有新增 summaries。
已有列表、详情页和 execution_info 改为批量数据库读取，保留结果/排序/游标语义。
运行 scripts/benchmark_task_queries.py 记录公开操作次数、SQL、字节及 p50/p95/p99；这是投影耗时，不是模型响应速度。
测试与交付证据见 [0.4.14 交付说明](../archive/pa-lite-0.4.14.md)。
