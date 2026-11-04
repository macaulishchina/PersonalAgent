# 任务上下文快照（0.4.17）

仅在新任务显式提供 `spec.context` 时使用。普通任务和子任务不会自动继承历史，也没有全局自动开启开关。公共 API 仍为 2，既有插件无需升级主版本；调用新能力的插件应声明核心依赖 `>=0.4.17,<0.5.0`，并声明使用 `ConversationRequest`、`TaskRequest`、`FileRequest`。

## 最小接入

```python
from assistant_runtime.api import ConversationRequest, FileRequest
from assistant_runtime.api.contracts import TaskRequest
import base64

snapshot = await context.runtime.invoke(ConversationRequest("snapshot", {
    "session_id": source_session,
    "request_id": snapshot_uuid,  # 调用前持久保存，响应丢失时复用
}))
if snapshot["state"] != "ready":
    # 查询同一 UUID 的 snapshot_receipt，不创建另一个快照冒充成功。
    ...

coverage = await context.runtime.invoke(ConversationRequest("snapshot_inputs", {
    "session_id": source_session,
    "snapshot_ref": snapshot["snapshot_ref"],
    "limit": 100,
    # "cursor": previous_page["next_cursor"],
}))

text = """前文是源会话历史，AI 的判断和操作属于原模型，并非你亲自验证。
你现在独立执行下面的新任务，旧待办不会自动成为本次要求。
[本次补齐背景]
……"""
background = await context.runtime.invoke(FileRequest("put", {
    "name": "task-context.txt", "kind": "file",
    "data": base64.b64encode(text.encode("utf-8")).decode("ascii"),
}))
# 持久保存 background["id"]，同一次任务创建重试不能换文件 ID。
task = await context.runtime.invoke(TaskRequest("create", {
    "request_id": task_uuid,  # 与 snapshot_uuid 分开，调用前持久保存
    "spec": {
        "title": "独立核查", "prompt": "本次要求，仍有 4,000 字符上限",
        "context": {
            "snapshot_ref": snapshot["snapshot_ref"],
            "additional_input": background["id"],  # 可省略
        },
        "execution": {"model": selected_model, "effort_mode": "model_default"},
        "tools": [],
    },
}))
```

插件通过自己的绑定上下文调用，不读取 PA 私有数据库或 Pi 会话文件。

主体说明和补齐背景由接入方组织为同一份文件。框架在首次模型调用前按“有效历史／压缩摘要／工具结果表示 → 追加背景 → 本次任务要求”装载，不增加一轮说明用推理。文件支持 UTF-8（允许 BOM），拒绝无效编码、NUL、图片及越权引用；大小仍为 2 MiB。该上限是接收限制，不保证任意内容都能装入目标模型。

## 快照与覆盖

`snapshot` 返回 `snapshot_ref`、`source_session`、`checkpoint`、`created_at`、`state`、`compacted`、`coverage_known`、`input_count`、`retention_days`、`expires_at`。两个引用都是不透明公共标识。创建快照不调用模型。

取最近已完整结束且持久化的检查点。源正在生成、调用工具、等待澄清或压缩时，使用此前检查点，不等待源当前轮结束。空会话可生成空基线；已有历史而无法确认检查点时报告不可用。旧历史不回填、不猜测覆盖，`coverage_known=false` 明确表示覆盖不完整。后续可记录确定的新输入。

`snapshot_inputs` 返回 `request_ids`、`next_cursor`、`coverage_known` 和固定检查点。源输入按完整轮次关联，澄清和补充归属原请求；当前未完成轮次不计入覆盖。覆盖表示来源关联，压缩后的细节可能已不再逐字存在。接入方维护业务讨论与 PA 请求的映射，并补齐缺口。

隐藏推理、供应商私有响应状态、源系统提示、工具配置和凭据不复制。大工具结果保留当时模型可见的节选和已有完整结果引用，按原授权持有依赖，不重新执行工具，也不把所有正文展开进历史。

## 幂等、状态和恢复

```python
receipt = await context.runtime.invoke(ConversationRequest("snapshot_receipt", {
    "session_id": source_session, "request_id": snapshot_uuid,
}))
task_receipt = await context.runtime.invoke(TaskRequest("receipt", {
    "request_id": task_uuid,
    # "spec": original_spec,  # 可核对 matches
}))
```

| 状态／错误 | 接入处理 |
| --- | --- |
| `not_found`、`found=false` | 权威存储确认没有创建记录；可用原 UUID 重试创建 |
| `preparing` | 检查点已经固定，等待或重试原 UUID；重启后也不换源背景 |
| `ready` | 可创建任务，接受时再次校验内容与依赖 |
| `unavailable` | 原准备失败，保留回执及错误；不要把新快照当作原请求重试 |
| `expired` | 内容已回收，回执和覆盖仍在；原 UUID 不重新抓取源会话 |
| `snapshot_storage_unavailable` | 无法确认存储，不能当作 not_found，不能换 UUID 自动派发 |
| 请求冲突／越权／内容损坏 | 明确失败；先核对参数、授权或存储，不重放历史操作 |

同一快照 UUID 不能改用另一个源会话。任务创建指纹包含原 `context` 引用；接受时保存检查点和追加文件摘要。任务 `context_info` 显示采用的关联，执行 `data.context` 显示首次装载事实。`context` 首版不可通过更新接口替换。上传本身没有创建 UUID，响应丢失可重传文本并使用明确返回的文件 ID；任务创建结果未知后必须先查原任务回执。

## 任务、容量和保留

一次性、定时、周期及长期 Agent 任务均支持；提醒和设备查询任务拒绝 `context`。每个新的调度执行从同一快照开始。暂停、补充、继续和显式恢复保留该执行自身上下文，不重新导入源快照；显式模型交接转移已核对的任务自身历史。未知执行沿用原核对机制，快照不是自动重派依据。

目标模型、深度、工作目录、工具及用量身份均独立决定；引用历史不继承源权限或预算。无 conversation 的安装仍支持普通任务，快照任务明确报告能力不可用。模型表示和图片支持受固定 Pi 版本及目标模型限制，失败不换模型或空背景。

导入时清除源请求的容量测量值，让 Pi 按新上下文重新估算并走现有自动压缩。只压缩任务副本，费用、准入、取消和失败沿用原机制。不承诺主体理解、压缩保真或缓存命中。

快照正文复用结果存储：默认永久保留，可配置 `work_files.result_retention_days=7/30/365`；默认总额度 512 MiB、单份 64 MiB。有效任务计划及活动／等待／暂停／未知执行保护背景和依赖。查询中的 `expires_at` 是无保护时的保留到期时间，实际可用性看 `state`；失效不自动换新快照。注册的追加文件维持既有保留策略。部署本功能不清理历史数据。

同快照的固定规则和历史表示保持稳定，动态运行身份在后部提供。不同供应商可能重排系统消息，缓存只能按实际遥测报告；缺失遥测为未知，不预热、不合并执行身份。

## 验证与部署边界

自动化：`python scripts/check.py --test test_context_snapshot --test test_pi_waiting --test test_input_files --test test_result_delivery --test test_task_models --test test_execution_service`。真实 Pi 合成场景不代表真实供应商验收。

真实模型：`python scripts/verify_context_snapshots.py --config /private/config.toml --models MODEL_A MODEL_B --output /new/evidence --confirm-paid`。仅复用配置中的 API 路由；在全新隔离实例运行，最多 4 次请求、每次输出 512 token，显式 token 额度和请求上限，无隐式重试。模型须支持 low／medium。未配置价格时金额为未知，不能把遥测中的零估算当作免费或金额上限。

交付说明和安装包验证不代表已经部署。实际服务切换另行按发布流程执行。
