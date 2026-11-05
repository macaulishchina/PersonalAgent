# 受控执行接入

本文描述公共 API 2 的受控执行接入。集成方负责业务权限、用户身份、项目基准和归档；PA 不引入项目角色。普通 Web／CLI 和原有单轮执行保持原工具策略，**不会因安装新版本自动变成沙箱**。

## 支持范围

| 能力 | 实现 | 边界 |
| --- | --- | --- |
| 持久上下文 | 创建、查询、续保、关闭；已确认单轮结束后继续 | 未知上下文不继续，不静默新建 |
| 单轮 | 独立请求、消息回执、事件、用量、操作、产物 | 稳定 UUID 去重；重复 ID 不改变意图 |
| 授权 | 宿主注入可信控制句柄，显式 owner 白名单，最长 60 秒租约 | 集成方在签发及每次续租前核验上游业务权限 |
| Windows 文件 | 固定工作区的只读 source／inputs、可写 work／artifacts | 相对路径、拒绝链接、写入需要旧摘要；文本单文件最多 4 MiB |
| Windows 检查 | LPAC 固定 cmd 脚本、子进程 Job、确认进程树停止 | 只验收固定合成 cmd 检查；实际解释器／编译器尚未验收，不开放任意 shell／构建脚本 |
| Linux | 原有执行能力保持 | 当前受控资源后端明确拒绝，不退回完整本地权限 |
| 产物 | 冻结、摘要、分段读取、保留／释放、归档确认 | 单件 64 MiB；实例发布存储上限默认 512 MiB |

Windows Server 2025 的本机证据见[隔离原型记录](../archive/windows-isolation-prototype-2026-10-26.md)。普通 Windows 未实机验收。后端需要 pywin32、NTFS ACL、创建 AppContainer profile 和管理自己资源 ACL 的权限；本机使用普通中完整性令牌通过，不安装服务、不申请提权。部署策略禁止这些操作时能力失败，不能降级。Windows OS 运行库及 `registryRead`、`lpacCom` 属于披露的运行依赖；不授权整个用户目录。不授予网络 capability，网络／远端服务与任意工具链不在本次隔离验收保证中。

## 1. 装配可信控制端

独立插件声明必需依赖 `execution`，资源声明 `execution_control`，通过：

```python
control = context.resource("execution_control")
result = await control.request("workspace_create", arguments)
```

实例显式配置允许的插件和资源所有者：

```toml
[execution.controllers]
my_bridge = ["owner-a"]
```

默认没有可信控制端，不支持通配 owner。句柄的服务身份由装配根绑定，不能从请求 JSON 传入。模型／工具调用上下文不能调用此控制句柄。插件必须在自己已认证的服务端控制通道中核验真实成员权限；不能把未经认证的 JSON 直接转交。已安装插件属于可信宿主代码，不防御恶意管理员或恶意插件。

受控模型仅开放 `pa_read`、`pa_write`、`pa_check` 和 `request_user_input`。不加载技能、MCP、codemode、原生 read/bash 等工具。模型参数不能携带 owner、actor、grant 或任意宿主目录。自定义业务工具须另行通过受控代理接入；直接 Python 文件操作和 `system_io` 不在隔离保证内，当前受控工具组不接受它们。

## 2. 身份和请求

所有时间为 UTC Unix 秒，期限须显式提供且有限；不替业务选择保留天数。context/workspace/grant/request/finalize/message 的 ID 使用调用端稳定 UUID，重试复用原 ID 和参数。

| 操作 | 参数 |
| --- | --- |
| `workspace_create` | `workspace_id, owner, version, usage_scope_id, expires_at`；可选 `usage_limits` |
| `workspace_prepare` | `workspace_id, area, path, text, expected_sha256`；area 为 source/work，null 摘要表示新文件 |
| `check_register` | `workspace_id, name, script, timeout_seconds`；ASCII cmd，最多 64 KiB，超时 1–60 秒 |
| `grant_issue` | `grant_id, binding, actions, expires_at` |
| `grant_renew` | `grant_id, generation, expires_at` |
| `grant_revoke` / `grant_get` | `grant_id` |
| `context_create` | `context_id, grant_id, expires_at, model, effort`；模型为 PA 配置别名 |
| `context_get` / `workspace_get` | `id` |
| `context_retain` / `workspace_retain` | `id, revision, expires_at` |
| `context_authorize` | `context_id, revision, grant_id`；仅空闲有效上下文，可换 actor，不可换资源绑定 |
| `context_close` / `workspace_close` / `workspace_collect` | `id, revision` |

`binding` 必须完整包含 `owner, actor, context_id, workspace_id, workspace_version, usage_scope_id`。actions 从 `execute, read, edit, check, publish` 选择。版本、用途、期限、owner 和控制端逐项匹配，知道 UUID 不是权限。context 的租约不能超过 workspace。第一次 context 创建后，source 基准和检查脚本冻结；换基准需新建 workspace。

签发去重不会续租。撤销不可恢复，换授权需新的 grant。启动后不恢复任何存活授权；必须经可信控制端重新核验并续租。时钟偏移超过 5 秒时拒绝旧租约。失联最长授权窗口为最后一次续租后 60 秒；新操作即时检查，在途 cmd 每约 50 ms 检查并终止，执行看门狗每 100 ms 检查。停止耗时另算，接收撤销不等于已停，必须查询核对证据。已开始的有界文件操作完成／退出前不释放工作区锁。

## 3. 开始和继续

```python
await control.request("context_run", {
    "context_id": context_id,
    "request_id": execution_id,
    "revision": context_revision,
    "text": "检查材料并给出修复建议",
    "limits": {"max_requests": 8},
})
```

可选 `inputs` 复用已有受归属检查的 FileRequest 附件引用。继续使用 `context_continue`，同样的字段，另加 `previous_execution_id`；必须是该 context 最后一个已确定且停止的 execution。每轮固定模型和思考配置，使用相同 Pi 上下文；模型路由变更导致原生上下文不能恢复时明确失败，不伪造恢复。

返回独立 execution 回执，内部 Pi session 不公开。同 context、同 workspace 都只允许一个在途轮次；跨 workspace 共享现有执行并发上限。切换、查询和控制连接断开不取消已接纳工作；租约失效仍会停止新访问。

通过同一控制句柄使用 `execution_get/events/observations/reconcile/cancel/supplement/answer/message_get`，字段对应[execution 契约](../../src/assistant_runtime/core/execution/README.md)。补充和回答需要 `message_id`，回答还要准确的 `question_id`。取消、查询和核对在授权被撤销后仍可用于该可信控制端拥有的执行。新增消息需要有效授权。普通 ExecutionRequest 不能绕过控制句柄访问受控执行。

`execution_operation_get` 接受 `id, operation_id, grant_id`，在新的有效 read 授权下读取已有受控操作回执；不会重做操作。操作记录关联轮次、父工具调用、grant 代次、actor 和工作区版本。

context 查询显示 `state, recoverable, expires_at, expired, last_execution, compression`。compression 是已观测压缩模型调用的状态计数，不是对模型记忆内容的保证。历史、原生上下文压缩和业务材料保留分别管理。

## 4. 用量与故障

仍使用 `ai_usage` 唯一账本。作用域由控制插件、owner、业务 `usage_scope_id` 一起确定，避免跨租户串账；单轮 caller 是 execution ID。普通推理和压缩均记入本轮和稳定范围。

workspace 的 `usage_limits` 是可选累计硬上限；轮次 `limits` 是单轮上限。支持 `max_requests, max_input_tokens, max_output_tokens, max_cost_cny, max_compactions`，金额单位沿用 CNY。未指定累计上限仍受原有 work／实例规则限制；单轮请求默认使用实例 ai_usage.max_requests，其余未指定项不额外限制。所有已配置上限同时满足才能派发，不自动添加额度。

同一作用域已登记的累计限额不可通过新 workspace／新 request 修改或刷新。请求/预留和历史归档都保留计数；未知费用不是零，无法可靠预留且配置了费用上限时拒绝调用。未知调用不自动退款。重试和继续不会清空已有额度。

| 状态 | 处理 |
| --- | --- |
| 活动或写操作未核实停止 | 查询、取消、核对；拒绝冲突执行及清理 |
| 原 execution unknown，资源已确证 fenced | 原结果仍 unknown；人工创建新 context／新 execution，可保留工作区修改 |
| 已确认终态且 context 有效 | 可显式 continue |
| 仅传输／归档失败 | 重读原 publication，不能再次 submit |

`execution_reconcile` 追加证据，不改写原结论。分别报告受控本地子进程、写入围栏、远端模型和外部副作用范围。未开放外部工具；不据本地停止推断远端停止计算或计费。宿主崩溃后若缺少可核验的旧进程树停止证据，保持 unknown／不可重试；当前实现不提供“用户确认已停”的布尔绕过入口。旧全权限执行也不能借此宣称安全重试。

## 5. 固定检查和产物

固定检查只接收 name，不接收命令行或模型提供的脚本文本。执行前校验受信脚本摘要，使用新 LPAC SID、最小环境、显式继承输出句柄和自有 Job；不依据 leader 退出猜测树已停。脚本 cwd 是 work，`PA_INPUTS` 是当前轮只读输入目录，`PA_ARTIFACTS` 是当前轮可写产物目录（均为相对路径）。其他轮次、其他工作区和宿主凭据不被授权。日志截断明确标记，检查输出最多 1 MiB，模型预览最多 16 KiB。

控制端在该轮已确认结束且资源围栏关闭后调用：

| 操作 | 参数（均须 grant_id） |
| --- | --- |
| `artifact_finalize` | `finalize_id, execution_id, name, expires_at` |
| `artifact_get` | `id` |
| `artifact_read` | `id`；可选字节 `offset, length`，单段最多 1 MiB |
| `artifact_retain` | `id, expires_at` |
| `artifact_release` | `id` |
| `artifact_acknowledge` | `id, sha256, bytes, archive_reference` |

finalize 的 name 是本轮 artifacts 中的单个文件名。冻结前拒绝链接和仍在写入的句柄，复制并 fsync 后才 published；同 finalize 意图返回原对象，不能覆盖。重启时未完成发布标记 unavailable，不重新复制可能已变的源文件。未知执行不自动发布，结果留作核对。

read 返回 Base64、字节游标、eof 和摘要，每次先验证完整快照。context 关闭后仍可用绑定同一资源的有效 read grant 归档产物。acknowledge 必须匹配摘要和大小，表示**可信归档方确认**，PA 不声称自行验证了外部存储。未确认显示 archived=false，读取完不自动变成 archived=true；归档失败只补传。释放／到期不能与读取抢删文件，收集保留去重墓碑。

## 6. 保留、迁移和验收

context 到期拒绝继续；续保需先续 workspace。活动／未知且未围栏的 workspace 不释放，不因 scratch 默认天数清除。关闭所有 context 后可 workspace_close，再显式 workspace_collect 删除该受控工作区；不清除已发布产物、用量和审计墓碑。已发布产物按自身 expires_at 收集。未知执行的引擎证据继续保留，不自动清理。

新增数据库表为增量建立，既有执行/会话/任务/账号不重置。旧事件缺少可证明的完整前缀时标记 history_origin_known=false。上线前仍按[发布流程](release.md)停服备份完整实例；回滚需要原程序、依赖和停服备份，不能用删除版本保护记录绕过。API 2 将 supplement/answer 改成必填稳定 message_id 并返回消息回执；API 1 外部插件需适配后重新声明，不能仅改数字。

当前验证使用独立临时资源和本地合成模型，不请求真实供应商、不切换运行实例。测试矩阵包括：Windows 文件与子进程隔离、重复取消、脚本篡改拒绝、授权/撤销/重启续租、同工作区互斥、真实 Pi 多轮与重启、禁止原生 shell、消息消费回执、事件缺口、预算预留与归档、不可变产物和归档确认。未验收实际业务工具链、供应商等价性、普通 Windows 或 Linux 原生受控后端；集成方须逐项接入和验收后开放业务修复。
