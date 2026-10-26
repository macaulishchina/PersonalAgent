# 受控执行与可保留上下文：联合设计

日期：2026-10-26。基线：`b9a402c`。状态：设计讨论存档。实现采用宿主注入的 ExecutionControl，操作名称和最终边界以[当前接入指南](../development/controlled-execution.md)为准；以下草案保留设计上下文，不直接作为调用示例。

本设计统一工作区授权、连续执行、消息回执、事件回放、停止核对和产物交付。集成方负责业务权限、项目基准、材料准备、工具业务和归档；PA 提供通用执行与证据能力。最新设计对齐要求优先于早期需求中的模糊描述。本文不选择业务预算金额、保留天数或生产部署权限。

## 1. 当前边界与实施顺序

| 能力 | 当前已有 | 本轮设计新增 | 首期不支持 |
| --- | --- | --- | --- |
| 执行 | Pi 单轮、请求去重、持久回执、准入和取消 | 稳定 context、多轮独立 execution、资源独占与授权 | 恢复未知轮次、自动重放 |
| 消息 | supplement / answer，绑定正在运行的轮次 | 稳定 message ID、持久回执、派发边界证据 | 保证供应商内部至多一次处理 |
| 事件 | 持久游标、每轮最近 512 条 | 明确截断水位、终态/空页/过期语义 | 无限事件留存 |
| 停止 | Pi 内部操作记录、Windows 进程树管理 | 公共分范围核对、启动身份、资源写入隔离代次 | 用本地退出证明远端停止计费 |
| 文件 | 已登记输入、摘要校验、执行暂存区 | 授权资源代理、不可变产物、分段读取、租约 | 以可变文件路径代表已交付产物 |
| 用量 | ai_usage 持久预留、共享 work 配额、未知不退款 | 单轮与稳定范围双重限额、轮次关联 | 更换 execution ID 刷新共享额度 |
| 隔离 | **现有执行拥有本地权限，Job Object 仅管理进程树** | Windows 受限工作进程与受控工具组 | 任意 shell、任意项目脚本、完整游戏构建 |

顺序：

1. PA-01 先完成真实隔离原型。固定合成检查 12 项已通过，尚未覆盖生产资源代理和实际工具链；见[原型证据](windows-isolation-prototype-2026-10-26.md)。**不能启用受控修复工具组。**
2. PA-03/04 可按本文身份与证据语义独立实现，不等待隔离通过，不据此开放文件修改。
3. PA-02/05/06 共用下述生命周期，再分模块实现和验收。
4. 先材料分析，后逐项开放修复和原上下文继续；不以功能开关掩盖未通过的安全边界。

## 2. 对象与责任

```text
受认证控制主体
  ├─ actor（发起/审计主体） + resource_owner（资源所有者）
  ├─ usage_scope（稳定计量范围）
  └─ grant（授权；issuer、service principal、资源版本、动作、有效期）
       └─ context（持久模型上下文，固定工作区版本）
            ├─ execution A ─ message / model turn / operation / usage / artifact
            └─ execution B ─ message / model turn / operation / usage / artifact
```

- **context**：PA 分配稳定 ID；绑定 owner、workspace ID/version、usage_scope、可用工具组和恢复状态。一个 context 同时只允许一个 execution；不同 context 仍受共享准入限制。Pi 原生会话标识仅存于适配层。
- **execution**：一次单轮，有独立 request_id、结果、输入、事件、用量和产物。接纳时固定本轮模型路由、工具集合和输入摘要。
- **grant**：不可由模型构造的授权引用，包含可信签发方、调用服务、审计 actor、owner、context/execution 绑定、资源版本、动作、有效期与撤销代次。路径及 work_id 都不是授权。
- **operation**：文件读取/编辑、检查命令、派生进程、模型请求和压缩调用分别登记；关联父 operation、execution、grant 代次、宿主启动 ID、进程创建时间及证据。
- **artifact**：某轮已发布的固定字节对象，独立 ID、大小、SHA-256、发布意图和可读/到期状态。
- **usage_scope**：集成方通过控制通道登记稳定范围；映射到现有 work 用量身份。actor、调用者、owner、用量归属允许不同，不能互相推断。

归属：`core/execution` 协调接纳、上下文与证据；`core/workfiles` 管资源、冻结与租约；`core/ai_usage` 管唯一账本；`core/pi` 管原生上下文和模型请求；`platforms/` 管 OS 隔离与进程身份。公共 `api/` 只放标准库类型与契约，不塞业务角色或平台实现。

现有 Web/CLI 会话保留原行为。外部执行上下文不默认加入个人记忆采集，不借用网页主会话、默认工作区或默认完整工具集。

## 3. 公共请求与响应草案

以下名字是拟定契约，**当前版本不能直接调用**。沿用公共 Request 类型；避免把 Pi 原生 RPC 暴露给集成插件。

| 请求 | 操作 | 关键输入/输出 |
| --- | --- | --- |
| ExecutionContextRequest | create / get / retain / close | grant 引用、workspace/version、usage_scope、期限；返回稳定 ID、revision、恢复/压缩/到期状态 |
| ResourceGrantRequest（控制面专用） | register / renew / revoke / get | 已认证的签发声明或引用；返回 grant ID、generation、expires_at、revocation 状态 |
| ExecutionRequest | submit / continue / get / cancel / reconcile | request_id、context_id、expected_revision；返回 execution 回执 |
| ExecutionRequest | supplement / answer / message_get | execution_id、message_id、text；answer 必须 question_id；返回消息回执 |
| ExecutionRequest | events | execution_id、cursor、limit；返回窗口、水位、缺口与事件 |
| ResourceRequest | read / edit / run_check | 不透明资源引用、相对路径或 check_id、版本条件；返回 operation ID 和结果 |
| FileRequest | finalize / get / read / retain / release | execution、finalize_id、资源引用或 artifact_id；返回不可变清单或字节段 |

控制请求只在可信插件/桥接的宿主控制能力上注册，不加入模型 MCP 工具表。模型工具不接受 actor、owner、grant、usage_scope 的覆盖参数；这些值由宿主不可变调用上下文注入。资源引用也必须验证归属，知道 ID 不意味着能访问。

创建及继续示例：

```json
{
  "action": "create",
  "arguments": {
    "request_id": "<uuid>",
    "workspace_id": "<registered-resource>",
    "workspace_version": "<baseline-version>",
    "grant_id": "<verified-reference>",
    "usage_scope_id": "<stable-scope>",
    "retention_policy_id": "<configured-policy>"
  }
}
```

```json
{
  "action": "continue",
  "arguments": {
    "request_id": "<new-uuid>",
    "context_id": "<context>",
    "previous_execution_id": "<settled-execution>",
    "expected_revision": 4,
    "text": "根据上一轮结果继续检查",
    "inputs": ["<registered-input>"]
  }
}
```

continue 产生新 execution；不是复活上一轮。模型、工具或工作区权限变化必须在安全边界重新验证。context 的 workspace baseline 不能原地更换。

回执分离结果与核对：

```json
{
  "id": "<execution>",
  "context_id": "<context>",
  "state": "unknown",
  "usage_scope_id": "<stable-scope>",
  "settlement": {
    "local_processes": "stopped",
    "local_writes": "fenced",
    "remote_model": "unknown",
    "external_effects": "not_used",
    "evidence_ids": ["<observation>"]
  },
  "allowed_actions": ["get", "events", "reconcile", "new_context_retry"],
  "artifacts": []
}
```

allowed_actions 是当前事实摘要，不能代替下一次操作时重新鉴权。new_context_retry 表示可以由控制主体显式创建新上下文；不隐含自动重试 API。

通用错误：`grant_invalid/expired/revoked/unavailable`、`workspace_version_conflict`、`resource_busy`、`context_expired/unrecoverable`、`operation_unsettled`、`idempotency_conflict`、`isolation_unavailable`、`usage_denied`、`artifact_not_ready/expired`。返回可诊断的对象 ID 和修复方向，不泄漏另一 owner 的资源是否存在。

同一可信主体及 owner 下，重复 request/message/finalize 意图返回原回执；同键不同内容冲突。首次接纳固定解析后的默认值，后续重试比较原输入意图和保存的快照，不因默认模型变化而误判为新执行。

## 4. 授权与撤销

首期采用**PA 登记的不透明授权引用 + 认证控制通道 + 短时有效租约**，不自制签名算法。

- 部署者登记可信 issuer、服务身份和其可代表的 owner/资源范围。控制桥通过现有宿主插件能力或单独受认证的管理通道提交；远程桥必须有身份认证与机密传输，聊天 token 不能充当授权签发凭据。
- PA 验证 issuer/服务身份、可委托范围、绑定和当前资源版本，持久化授权。业务系统负责证明 actor 的项目权限；PA 不解释项目角色。
- grant ID 是引用而非 bearer secret；查询/使用仍要求匹配控制服务、owner 和上下文。
- 接纳、continue、工具授权以及每个新副作用前，检查有效期、撤销 generation、资源版本和租约。文件读取同样检查访问权限。
- 撤销事务先推进资源 fencing generation，阻断新的操作与旧回调，再取消所属在途操作，追加核对记录。已收到 revoke 和实际停止分别返回。
- 不支持离线续租。提议授权租约默认 30 秒，部署可调小，上限 60 秒；签发服务断联不续租。业务权限撤销在 PA 尚未收到时的接纳窗口最多为剩余租约，**不代表在途进程已在该窗口内停止**。
- 续租必须重新核验上游权限。有效期使用 UTC 持久化并辅以进程内单调时钟；时钟异常或重启后未完成核验时禁止新副作用。
- 新轮不得借 context 引用扩权。新的 grant 必须与同一 workspace version/owner/scope 匹配，且不能让旧 generation 回调继续写。

资源排他锁、操作接纳和 fencing 检查要由同一协调边界串行化，避免“检查授权后撤销、随后仍启动进程”。已启动的原生进程不能靠数据库 fencing 阻止系统调用；须由隔离后端终止/冻结并核对，期间工作区仍不可复用。

## 5. Windows 隔离方案与门槛

首选验证路线：**宿主资源代理 + 每轮 LPAC 受限进程 + Job Object 生命周期管理**。LPAC 是候选机制，未被当前证据证明满足完整交付。

- 模型文件工具由代理基于目录句柄访问；拒绝绝对路径、跨根跳转、重解析点、设备路径和未经批准的链接。校验后到实际 I/O 的对象身份必须稳定。
- 模型编辑使用受控替换，校验 expected revision/hash；文件访问登记 operation。不能只做字符串 startswith。
- 检查命令由可信部署配置指定绝对可执行文件、版本/摘要、允许参数、环境、读取根、输出目录、超时、子进程规则；模型只给 check_id 和受约束参数。
- 可执行文件/脚本及工具链不得由模型写入。若检查程序会自动加载项目脚本、插件、启动文件或用户配置，必须禁用或单独纳入授权和验收。
- 进程在受限令牌下挂起创建，加入宿主 Job Object 后才恢复；后代不得逃离 Job。Job Object 不能替代文件权限隔离。
- 仅传入白名单环境变量和必要句柄；不得继承宿主 API key、SSH 配置、控制面通道、开放文件句柄或无约束 MCP 能力。
- 可写目录限当前授权工作区和本轮 temp/check/staging；容器 profile 视为本轮临时资源，不得成为跨轮共享缓存。
- 只读材料、源码及运行库通过部署清单列出。LPAC 的系统默认可读资源、注册表/COM 能力也必须单独披露；不能把“有 LPAC”解释为只有清单路径可读。
- 原生检查程序不直接取得模型凭据或网络能力；模型推理由宿主 Pi 传输完成。远端服务和外部副作用使用独立授权适配器，首期不在本地文件隔离保证内。

插件是可信宿主代码，但受控工具组只接纳经审查、模型驱动 I/O 全部走 ResourceRequest 的工具。直接 Python 文件访问、任意 system_io、任意外部 MCP 工具不得在该组启用。现有完整权限执行不能成为隔离失败后的 fallback。

部署门槛：当前普通用户已能创建/删除测试 AppContainer profile 和配置自有目录 ACL；**固定合成 cmd 检查已通过；实际工具链尚未验收**。生产需要哪些运行库、注册表能力、服务/专用账户或管理员安装，须随下一次可运行原型清单确定。在原型门槛通过前，不承诺普通用户部署即可交付。不得为排障自动授权整个用户目录或扩大网络能力。

Windows Server 为首期验证平台；桌面 Windows 使用共享实现但需后续单独实测声明。Linux 尚无本项隔离后端，受控请求返回 isolation_unavailable，保留原有普通执行行为。若 LPAC 无法满足明确资源范围，必须另交专用隔离 worker/虚拟化方案及安装要求，不以降级换成功。

## 6. context、unknown、取消与重试

上下文有独立的恢复状态 `ready / active / uncertain / expired / closed`。结果终态不等同于资源可释放。

| 事实 | 行为 |
| --- | --- |
| 活跃或冲突操作未核实停止 | 只允许查询、取消、核对 |
| 原结果 unknown，写入已隔离且冲突操作确定停止 | 保留 unknown；允许人工新建 execution 和 context，可保留同一工作区修改 |
| 结果确定、进程已停、上下文可恢复且授权有效 | continue 创建独立新 execution |
| 仅归档/传输失败 | 补传原 artifact，不重新模型执行 |
| 上下文到期/原生恢复不可证明 | 拒绝 continue；显式新建上下文，不静默空白重建 |

同一工作区版本首期只允许一个写入执行，包括跨 context；并行只读的放宽不是首期要求。资源引用仍活动或停止未知时保留占用，清理不释放此约束。

reconcile 追加 observation：观测时间、host_boot_id、operation、进程创建身份、核对范围、证据来源和结果。PID 不单独作为身份。宿主重启若无法核实旧进程与外部副作用，保持 unknown/不可重试。

本地进程结束、远端请求状态、外部工具副作用分别记录。纯远端模型计算未知可以保留费用不确定性，在本地写入及全部冲突副作用安全后允许人工新轮；有冲突外部副作用未核实则继续阻断。核对不得修改原 unknown 为 failed。

人工 retry 由集成方提供材料、已有结果和修改说明，使用新上下文；不自动重放工具、reset 工作区或应用旧 patch。晚到结果只追加审计/候选证据，不能覆盖既定结果或写入已更换代次的工作区。

## 7. 消息回执与事件

消息回执状态：

- accepted：持久化，并排队到指定 execution；尚未证明进入模型输入。
- consumed：持久记录 message_id → execution → turn/model_request 的关联，并有适配器证据证明加入该次请求输入。只说明输入归属，不说明理解/完成。
- not_consumed：能够证明尚未派发且本轮已结束/取消。
- unknown：派发和证据落盘之间发生异常，无法证明是否纳入输入；禁止自动补发。

采用写前登记派发意图、适配器显式确认输入纳入、落盘回执的协议。无法让本地数据库和远端服务形成原子事务，崩溃窗口诚实标 unknown。相同 message_id 至多追加/派发一次；不得把 unknown 回执当作发送失败自动再试。answer 还须校验 question_id 和轮次；迟到答案不流入下一轮。

事件响应示例：

```json
{
  "items": [],
  "cursor": 1200,
  "latest_cursor": 1200,
  "earliest_cursor": 900,
  "deleted_through": 850,
  "gap": false,
  "execution_state": "completed",
  "has_more": false
}
```

继续沿用持久游标；必须按 execution 保存已删事件的最高水位。不能用全库 sequence 的数字空洞推断丢事件。请求 cursor 小于本轮 deleted_through 时 gap=true，返回可读窗口；客户端按回执重建快照，不能假装事件完整。单页查询固定上界，空页不等于执行完成。

初始保留当前 512 条/轮、每条 16 KiB、每页最多 100 条；超限 payload 保留事件 kind、对象关联及截断标识，正文可作为受控附件引用。回执/截断水位不跟着事件滚动删除；到保留期后返回明确 expired，不能伪装成空历史。精简 tombstone 的保留周期另由执行记录策略控制。

## 8. 用量与保留配置

以下是待实现的配置语义，不是可粘贴的当前 TOML。仍使用统一配置服务、显式 include 和当前 ai_usage 账本。

| 配置 | 提议默认/语义 | 生效 |
| --- | --- | --- |
| 隔离后端 | 未通过探针时 unavailable；不得自动选 full permissions | 进程/工具装配变更维护生效 |
| 检查工具清单 | 空；逐项配置后验收 | 新执行采用新 revision |
| grant 租约 | 30 秒，最多 60 秒；失联不续租 | 新/续租；不延长已撤销 grant |
| 每轮限额 | 未填写则继承现有有效保护，不另赠配额 | 新准入；降低不重放已派发调用 |
| 稳定范围累计限额 | 复用现有 work allowance；附加金额/token 限额可选 | 新准入；更换轮次不清零 |
| context/workspace/artifact 保留策略 | **不预设业务天数**；受控入口须选部署策略或明确 expires_at | 延长需授权；缩短不清理活动资源 |
| 暂存空间配额 | 受控发布启用前须配置字节限额；不足明确拒绝 | 下一次接纳/发布 |
| 事件窗口 | 当前 512 条、16 KiB/条、100 条/页 | 后续写入/查询 |

配置无受控保留策略时，可以继续普通材料流程，但拒绝新建受控持久 context；不暗用 scratch 的默认 7 天。期限必须有限，临近到期可 retain，close/release 降引用但不绕开在途占用。

用量单位：请求次数、输入/输出 token、压缩次数、秒和当前账本 CNY 成本。单轮、稳定范围、账户/实例适用限制必须同时满足，使用同一次原子预留；不能先扣一个后发现另一个失败而遗留半次接纳。

已有默认 work 请求额度按稳定 scope 计算，单轮仅消耗其剩余值。未知 token/价格保留未知与保守预留；已启用且无法给出安全上界的限额拒绝新付费派发，不按零计账。只有受信任维护操作可以增额，必须留审计。

模型请求和实际上下文压缩均关联所属 execution；不在账外预热/压缩。模型选择或上下文变化不重置用量。脱离轮次的维护模型调用若确有需要，须有独立可审计操作和明确用量归属，否则不执行。

保留对象分别处理：

- context：到期拒绝继续，公开原生上下文是否可恢复、压缩事实、期限。关闭不删除业务工作区。
- workspace：集成方控制基准和保留，PA 只释放本轮租约；context、活动操作和未核实操作的引用阻止清理。
- PA artifact：published/expired/readable 独立于 context；传输时持有短租约。仅“下载完成”不表示集成方已可靠归档，归档确认单独记。
- 已归档副本：由集成方独立保留，不受 PA context 关闭影响。

清理先以事务获取资源删除权，确认无活动/未知占用、无引用且已到期，再执行物理删除并登记结果。retain/finalize/读取租约与清理竞争同一 revision；删除开始后不允许“续保成功但文件已没了”。

## 9. 产物冻结

沿用 workfiles，新增独立不可变对象区和清单，不把 scratch/artifacts 中的动态文件直接对外交付。

1. finalize_id 固定发布意图，关联 execution 和文件集合；验证授权及全部冲突写操作已停止。
2. 持源文件句柄，拒绝链接/重解析点，Windows 使用禁止写入/替换的共享模式。若无法获得一致快照则返回 busy，不靠复制前后 hash 相同推断没有竞态。
3. 复制到宿主私有 staging，同时计算大小和 SHA-256，flush 后在同卷原子发布；模型进程不能写已发布对象区。
4. 事务提交不可变 manifest 与引用；返回 published 前确认字节和元数据一致。相同 finalize_id 返回原引用，不同内容/意图冲突，不覆盖旧对象。
5. 新轮同名文件必须获得新 artifact ID。内部可按内容寻址复用 blob，但访问权限和引用仍按 artifact/execution 分离。
6. 崩溃后依据发布意图和 manifest 核对 staging/blob；未提交不能当作完成。孤儿文件按受控回收规则处理，不重跑模型。
7. read 支持有界 offset/length，返回总大小、摘要和不可变版本；客户端断线可继续拉取并验证完整摘要。授权过期、对象过期和空间不足分别报错。

Windows 外的文件冻结实现需按平台单独验证，不能把 POSIX rename 等同于完整的写入隔离。外部工具自行发布的产物可以走外部文件服务，但必须遵守逐轮不可变和完整交付语义。

## 10. 验收矩阵与交付门禁

“代码已有”仅指本轮只读复核，不表示本轮重跑了运行验收。

| 项目 | 关键验收 | 当前证据/门禁 |
| --- | --- | --- |
| PA-01 文件与命令隔离 | 允许读/写正例；拒绝其他项目、假凭据、只读写入；链接/换路径竞态；子进程/撤销 | 固定合成检查 12 项通过；生产后端及撤销待实现 |
| PA-01 部署 | 普通/受限账户、明确工具链依赖、权限不足 fail closed、无宿主秘密继承 | 当前普通用户 profile 成功；完整部署待验 |
| PA-02 上下文 | 多轮 Pi 上下文、压缩关联、同上下文串行、跨上下文不串线、过期拒绝 | 可复用 Pi 能力；公共生命周期新增待验 |
| PA-03 消息 | 重复 ID、迟到 answer、取消竞态、派发前后崩溃、unknown 不补发 | 已实现并通过定向测试和真实 Pi 合成模型验证 |
| PA-04 事件 | 超过 512 条、跨 run 游标空洞、空页/终态、payload 截断、重启/过期 | 缺口/水位/旧记录来源已实现，定向测试通过；期限策略待接入 |
| PA-05 核对 | PID 复用、宿主重启、后代仍活跃、旧回调、纯远端未知、冲突外部副作用未知 | 原生操作及进程观察已记录；公共核对保守返回未隔离/未知；停止证明与 fencing 待实现 |
| PA-06 产物 | 下一轮同名、重复 finalize、仍在写、替换/链接、复制/提交各崩溃点、分段断线/归档 | Windows 冻结/分段/期限/配额已实现并定向验证；授权与归档确认待接入 |
| 授权 | 伪造/越权 issuer、错误 owner/version、过期/撤销/失联、撤销与启动竞态 | 新控制面待实现 |
| 用量 | 单轮及累计双限额、换 ID 不重置、压缩收费、未知费用、重启预留 | 原有账本可复用；组合限额待验 |
| 保留 | retain/release 与清理竞态、活动/unknown 不删、关闭 context 后已归档仍可用 | 新统一引用生命周期待实现 |
| 平台 | Server 原生、桌面 Windows 共享逻辑、Linux 不支持时明确拒绝 | 本轮仅 Server 有限原型，无跨平台认证 |

每项验收先用合成材料和本地合成模型；真实供应商只验证其实际传输/用量行为，不替代本地隔离测试。通过一项只开放对应能力，不一并开启 repair/continue。

## 11. 迁移与实现落点

新增表/字段采用可重复、可回滚的增量迁移。原 execution 回执不改 ID、不补猜 context、grant 或原生 turn 关联；旧执行显示“不可公共继续/无受控隔离证据”。不自动改业务文件或现有实例工具列表。

只引入一套当前公共契约，不维护第二套执行器或兼容别名。版本/API 变更按[发布规则](../development/release.md)在实际实现交付时确定；本设计不修改软件版本。

已修正初版探针的 LPAC 属性常量与启动方式，固定合成检查及后代限制通过；详情见[更新的原型证据](windows-isolation-prototype-2026-10-26.md)。下一步实现受控资源代理与授权、上下文及核对生命周期。原型不能通过则隔离功能保持关闭；PA-03/04 的实现不依赖这个问题。生产检查工具清单、部署权限、业务保留策略和预算值由集成方在装配前提供，不要求现在决定。
