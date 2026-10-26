# 插件开发规范

先阅读[架构](architecture.md)了解职责和数据流。本页维护公共接口与插件契约；可运行示例见[独立插件示例](../../examples/sample-plugin/README.md)。

## 平台基础设施

`platforms/` 集中提供文件锁、私有文件、目录句柄、进程所有权、本地 IPC 和服务集成。
公共契约位于 `api/system.py`，包启动时只绑定惰性后端；Portalocker、pywin32 等依赖
在使用时加载。内核不导入平台实现。应用和执行适配器负责装配，事务恢复、执行状态、
额度及重试仍由各自插件拥有，平台层不得实现业务策略或第二套 Agent 循环。

独立插件声明 `resources=("system_io",)`，通过 `context.resource("system_io")` 使用
公开设施；该资源不提供宿主、配置全集或插件注册表。文件须位于插件自己的私有目录
或已授权工作资源中。文件锁持有期等于打开文件的生命周期；创建凭据应使用
`atomic_write`，不能认为 Windows 的 `chmod(0600)` 已实现隔离。

扩展插件通过静态 `plugin.json` 的 `supported_platforms` 声明平台认证，运行时声明
从同一文件读取。单文件插件使用 `<模块名>.plugin.json`。每个扩展分别认证；
现有全部内置扩展声明支持 Linux、Windows，独立示例包仍只认证 Linux；本机音频 `pipewire`、本机摄像头 `v4l2` 在插件内部选择平台实现。Linux 使用 PipeWire、V4L2，Windows 使用 WASAPI、DirectShow，不另注册同能力的 Windows 插件。
当前矩阵见[模块索引](../reference/modules.md)与 `pa plugins`，不以插件属于扩展为由统一禁止 Windows。`windows` 同时表示普通 Windows 和 Windows Server。认证是随发布维护的支持声明，不是签名或在线认证服务。
独立包必须在 entry point 模块对应路径打包上述元数据；缺失、无效或与运行时声明不一致
均不能启用，不再对扩展自动假定跨平台支持。管理页无需导入扩展代码即可展示认证。
配置保存和启动装配都检查当前平台，未认证时拒绝启用。Windows 默认使用 slim，
显式选择 standard 时先按所含插件的实际认证检查，未获认证则在创建实例目录前拒绝。已有显式配置不会自动删改。
Windows 适配完成度和认证启用策略分开记录，
操作步骤见[安装指南](../guides/deployment.md)，实测边界见[平台验收记录](../archive/platform-acceptance-2026-10.md)。

公共接口为 `assistant_runtime.api`：`PluginDefinition`、`Manifest`、
`PluginContext`、`CallIdentity` 和 `Tool`。仓库内和独立包采用同一声明模型。

### 平台身份与默认实现

运行时提供不可变的 `PlatformInfo`，内置及外部插件共享同一个平台对象，均可读取 `context.platform.name`（`linux` / `windows`）；普通 Windows 与 Server 共用实现。平台由系统识别，配置不提供伪装平台的开关。

没有平台差异的插件保持一份实现，只声明 `supported_platforms` 即可，无需添加平台分支或 `BackendSpec`。仅有系统调用差异时可在插件内部按 `context.platform` 适配；存在需要独立选择的实现时才使用下面的默认实现协议。

需要多实现的插件在同一静态 `plugin.json` 中声明 `backends`（平台到实现名列表）和 `default_backends`，运行时 `PluginDefinition.backends` 使用公开的 `BackendSpec`；`plugin_backends()` 可直接从该文件读取。目录显示无需导入运行库。静态与运行时声明不一致则拒绝加载。

配置读取、配置服务、CLI 清单和装配器复用同一静态认证与默认实现检查，内置和外部插件规则一致；检查先于插件代码加载及实例创建。插件在工厂中读取 `context.backend`，仅导入被选中的实现。配置优先，其次插件声明的默认值；只有一个实现时可自动选中。多个实现且没有默认值、指定实现不可用或平台不匹配均明确报错，不静默回退。

```toml
[plugin_backends.pipewire]
linux = "pipewire"
windows = "wasapi"

[plugin_backends.v4l2]
linux = "v4l2"
windows = "directshow"
```

这些是平台内的默认实现，不是设备选择；设备身份、凭据和端口仍由实例显式配置。未启用插件及其他平台的默认值允许保留，禁用插件时也可一并删除其实现偏好。网页“功能与插件 → 平台默认实现”只为已启用且当前平台存在多个实现的插件展示选择器；保存了无效选择时仍显示修复入口。单一实现自动选择，不增加无意义的表项。CLI `pa plugins` 显示当前实现。等价默认值或其他平台设置的变更不替换运行模块；本机适配器切换通过已有租约与维护边界，固定主语音实现和外部插件实现切换须显式重启。

## 版本与兼容声明

每个插件只有一份 `plugin.json`，单文件插件使用 `<模块名>.plugin.json`；它同时拥有版本、平台、依赖与可选实现信息。配置界面描述仍放在 `configuration.json`。外部包的插件版本必须与其 Python distribution 版本一致，同包多个插件共用该包发布版本。不要保留另一份旧 `platform.json`。

```json
{
  "schema_version": 1,
  "id": "sample",
  "version": "1.1.0",
  "api_version": 2,
  "requires_core": ">=0.4.0,<0.5.0",
  "supported_platforms": ["linux"],
  "requires": {
    "execution": ">=0.4.0,<0.5.0",
    "workfiles": ">=0.4.0,<0.5.0"
  },
  "optional": {},
  "critical": false
}
```

上述声明对应仓库的独立示例包。`id` 必须等于 entry point 名称；`version` 使用三段稳定数字版本；`api_version` 必须与宿主公共协议一致。`requires` 和 `optional` 即使为空也须显式声明。平台、版本范围和依赖均由插件作者验证维护，不能由安装器猜测。

插件声明使用 `from assistant_runtime.api import plugin_manifest`，将 `plugin_manifest(__file__)` 传给 `PluginDefinition`。声明缺失或错误、包版本不一致、内核／协议／依赖版本不兼容都拒绝加载；静态清单先于运行代码加载，运行时再核对 manifest。范围使用稳定版本比较 `>=`、`>`、`<=`、`<`、`==`、`!=`，逗号表示同时满足；内核范围必须有上界。可选依赖未启用时不要求存在，已启用时同样检查版本。

核心插件的 `version`、`requires_core` 为 `"core"`，核心间依赖可使用 `"core"`，均解析为当前内核版本；外部插件不能使用这些内部标记，也不能声明 critical。扩展无需实现版本比较、在线认证或升级回调。兼容检查复用现有配置保存、装配和维护入口；内核只负责依赖图及生命周期，不引入包管理库。

### 新建或迁移插件

1. 新独立插件优先采用目录式包：entry point `sample = "personal_agent_sample:definition"` 对应 `personal_agent_sample/plugin.json`。从旧接口迁移时，把 `platform.json` 的字段移入此文件并补齐版本声明，删除旧文件。
2. `definition()` 使用 `plugin_manifest(__file__)` 生成 manifest，平台通过 `assistant_runtime.api.plugins.plugin_platforms` 读取同一文件；存在多实现时再使用 `assistant_runtime.api.backends.plugin_backends`。保留自己的配置、资源、工具和能力声明，不在工厂或运行实例中重新写 `Manifest(... version=...)`。
3. 确保 wheel 真正包含声明文件；只在源码中创建 JSON 不够。示例包使用如下打包配置，版本与 `plugin.json` 一致：

   ```toml
   [project]
   name = "personal-agent-sample"
   version = "1.1.0"

   [project.entry-points."personal_agent.plugins"]
   sample = "personal_agent_sample:definition"

   [tool.setuptools.package-data]
   personal_agent_sample = ["plugin.json", "configuration.json", "skills/*/SKILL.md"]
   ```

4. 在独立验收环境安装 wheel，检查 `pa plugins` 的版本、平台及依赖，再按[版本专项检查](testing.md#版本与兼容性检查)验证拒绝与正常加载场景。发布时按[版本规则](release.md#版本规则)递增版本；修改兼容声明也属于发布变更。

单文件插件的静态资源是 `<入口模块>.plugin.json`。`plugin_manifest(__file__)` 默认读取同目录 `plugin.json`，仅对 `<名称>_declaration.py` 自动选择 `<名称>.plugin.json`（例如内置 gesture）；不要假定任意单文件入口都能这样定位。其他单文件布局可使用公共 `assistant_runtime.api.releases.read_descriptor` 与 `manifest` 显式读取对应路径。独立包元数据必须位于 entry point 模块对应位置，不能靠导入插件后查找文件。

业务配置和实例插件列表不自动改写。缺少新声明的旧插件无法启用，但可在管理页查看并取消；宿主不提供旧声明的隐式兼容层。升级、失败恢复和数据格式边界见[发布与切换](release.md)。

## 声明与生命周期

每个仓库内插件的 `plugin.json` 拥有静态版本、依赖及平台信息；`declaration.py` 读取它生成 manifest，并声明配置校验、工厂、能力、资源、技能和工具服务。`owners.json` 只登记内置插件的身份与模块位置，不重复登记版本、依赖或工具名。
独立包通过 `personal_agent.plugins` Python entry point 声明，名称与 manifest 一致。
只导入显式启用的独立包，不自动安装或更新。外部包不能占用内置身份、声明关键插件，
或使用核心内部工厂。

`Manifest` 从静态描述生成，包含插件版本、框架接口版本、必需／可选依赖及版本范围。
缺失、循环和版本冲突在启动前报错。必需依赖启动失败时，依赖方为 blocked；
可选依赖不可用时，通过健康信息说明。关闭按依赖反序进行，取消已接收调用和
后台工作，再清理资源、能力处理器及订阅；迟到结果不能覆盖新的执行状态。

核心插件可声明 `internal_factory` 使用框架内部接口；扩展插件只能使用公共上下文。
扩展插件不得保存宿主对象、读取完整配置、访问全局数据库或其他插件私有实现。

## 公共上下文

上下文提供自身校验后的配置、私有目录、命名空间状态、声明的凭据引用、
日志、事件、后台工作与清理、取消信号和全局就绪事件。

通过 `uses` 声明使用的请求类型，通过 `provides` 声明提供的类型；使用
`context.invoke` 和 `context.register` 调用/注册。独立插件的调用者身份由宿主绑定，
不能通过工具输入选择其他任务身份。直接调用、MCP 和后台执行都保留同一个工作额度。

设备注册和用量准入等直接资源须显式声明；未知资源在预检报错。设备目录注册句柄
限定当前所有者，资源注册句柄不提供任意插件实例或全量 provider 字典。
付费请求必须经过公共用量准入，或通过已受计量的 models 能力；不允许自行补发、
保温、重放未知结果，或为子任务/模型切换创建新额度。

内置业务仓库只接收由框架注入的自身仓库连接。独立包可在私有状态目录创建自己的
持久化存储，不能要求获得框架全局连接。共享数据库的既有表结构保留，表和查询的
实现归各自所有者；例如网页通过对话入口接口读取附件，通过任务能力取得交付文件。

## 单次执行能力

启用 `execution` 核心插件后，扩展插件可以声明 `uses=(ExecutionRequest,)`，
通过同一个公共上下文提交、查询、补充、回答澄清或取消执行。请求身份与共享用量
由宿主绑定，不允许在请求参数中自行指定。支持文字及登记文件输入；slim 使用同一宿主，
默认启用公共对话与 CLI，个人助手、设备、语音、任务和通知保持关闭。
详细操作和验收范围见[执行模块](../../src/assistant_runtime/core/execution/README.md)。

## 公共对话与入口

`conversation` 依赖 `workfiles`、`execution`，不依赖个人助手；`assistant` 和 `cli`
依赖公共对话，`web` 仍是扩展。对话拥有持久化会话目录、入口绑定、历史、附件、队列与结果投递。
个人助手的提示、语音同步、播报及设备状态协调留在 `core/speech`，仅启用时装配。
公共 `ConversationRequest` 提供 create/list/open/history/events/state/submit/queue/supplement/answer/cancel，
只接收逻辑会话 ID、请求 ID、问题 ID 与事件游标，入口不操作 Pi 原生会话。

同会话普通消息顺序执行；跨会话、单次执行与后台任务共享已有准入和用量保护。
补充、回答和取消绑定指定轮次，迟到请求不得影响下一轮。会话保留 Pi 上下文，
不按消息创建一次性执行器。退出终端或切换会话只改变入口，不取消已接收工作。
网页绑定主会话；CLI 每个客户端拥有当前选择，同时保存该操作者下次连接的默认选择。
自动启动与服务身份检查在 `platforms/`，业务与工具子进程仍由现有管理进程拥有。
迁移和重启恢复禁止重放未知操作。详见[对话模块](../../src/assistant_runtime/core/conversation/README.md)与 [CLI 模块](../../src/assistant_runtime/core/cli/README.md)。

## 工具、技能和安装记录

工具需包含名称、说明、输入/输出 JSON Schema、处理入口和独立的读写、破坏性、
幂等性、外部访问属性。这些属性不代替权限校验，也不等于费用许可。
内置工具的唯一声明在所有者的 `tools.py`；MCP 和直接业务入口共用能力处理器。
独立插件返回 `Tool` 声明；直接工具调用和动态 MCP 投影使用同一注册表及 Schema。
Pi 主会话和后台任务自动获得动态扩展工具服务，不需要改中央工具名单。

技能随所属插件打包，由其声明选择；禁用插件不加载技能。技能重名或实例覆盖会报错。
`.agent`、`.agents` 与根目录开发指南不作为助手技能加载。

`pa plugins` 静态列出全部可发现插件的类型、版本、启用状态、依赖范围、平台实现和不兼容原因；有启用项被阻断时退出码为 2。启动时的 `state/plugin-installation.json` 是装配清单，成功运行及降级保护另由 `state/release-state.json` 记录，不把装配完成等同于启动成功。插件启停、安装和升级需要显式重启；普通设置按所属模块声明的更新方式生效。
这是一套可信代码开发契约，不是 Python 或操作系统沙箱。

## 示例和验证

`examples/sample-plugin` 是可独立构建的包，包含配置、读写工具、技能、事件和状态。
安装后在实例 `plugins` 中加入 `sample`；可用 `[plugin_settings.sample]` 配置
`max_length = 1000`，该值控制写入工具的输入长度。

自动检查覆盖：模块导入边界、依赖阻断、清理与取消、真实 wheel entry point、
动态 MCP、请求身份、同一工作预算，以及真实 Pi 子进程访问合成服务。
新硬件或新模型仍需要其独立的实际验收，不能从这些测试推定已经支持。

## Discovery metadata

MCP tools are deferred by the Pi adapter and found through native `tool_search`. Keep
names and descriptions specific, concise and searchable; put a useful first-line summary
before detailed instructions. The native ranker indexes names/descriptions/argument
metadata and tokenizes English identifiers. Use stable English tool identifiers even
when surrounding user-facing text is Chinese. Avoid duplicate giant examples or whole
manuals in descriptions; package operational guidance in skills and return large data
through pagination/file references. Plugins do not activate tools themselves or alter
the caller's scope. Deferred exposure changes visibility, not authorization.

## Capability deadlines

A provider declares its default deadline with `context.register(RequestType, handler,
timeout=seconds)`. Callers use `await context.invoke(request)` to inherit the owner's
default; an explicit shorter timeout or host-bound tool deadline can still cancel it.
Defaults must account for the real operation: model inference owns its configured
600-second budget, rather than inheriting the kernel's 15-second management default.
Deadline registration is removed with the capability on shutdown or failed startup.
Transport return margins do not expand the provider's inner operation budget.

## Configuration descriptions

An extension can declare `configuration=ConfigurationSpec(...)`. The installed-plugin
adapter applies schema defaults and validates basic constraints before invoking the
owner's semantic validator. The frontend must use this same description, not duplicate
its defaults. Schema errors identify the field and rule without echoing secret values.

Package a static `configuration.json` alongside the package used by the entry point.
It contains `version: 1`, `namespace`, `title`, `group`, `description` and `schema`.
The entry point reads the same resource to construct `ConfigurationSpec`; the operator
catalog reads its installed package-data file without calling the entry point. Include
it explicitly in setuptools package data. The sample extension demonstrates this path.

Default resolution fills declared properties only inside objects that already exist,
unless the object itself has an explicit default. Optional features are not instantiated
merely because their children have defaults. Semantic validation remains plugin-owned.
Both presets include the independent operator console; see the [configuration module](../../src/assistant_runtime/core/configuration/README.md).

The generic operator form supports schema `title`, `description`, `enum`, numeric bounds,
objects/lists, `readOnly`, `writeOnly`, `x-advanced` and `x-group`. The owner supplies these
annotations; the frontend contains no copy of plugin defaults. `sensitive` and `read_only`
paths are relative to the owner's namespace and may use `*` for named-object entries.
For external packages the host always binds configuration under `plugin_settings.<id>`;
a package cannot declare a different owner's namespace. Metadata and runtime schemas must
agree when configured plugin settings are validated.

Use `writeOnly: true` for a secret even if its name does not resemble a credential.
Secret defaults/examples are not returned by the management API. Browser writes use
`set`/`default` for ordinary fields and `replace_secret`/`clear_secret` for secret fields.
`default` removes an override; it does not copy a rendered default into TOML. Parent table
replacement is refused when it contains declared secrets. Array/dictionary editing uses
a structured editor when no more specific schema is declared.

A static `schema.x-listeners` list can identify TCP bindings, each with `host` and `port`
property names and an optional boolean `enabled` property name. Startup preparation checks
these against the independent management listener without opening ports or probing devices.
Configuration descriptors must remain static package data; arbitrary frontend scripts and
runtime network probes are not part of this interface.

### Pages and live updates

Keep presentation separate from the data schema. `interface` describes the independent
plugin page and maps fields to host-owned components; `application` declares the update
mode. Example:

```json
{
  "interface": {
    "title": "Example connection",
    "fields": {
      "endpoint": {"order": 0, "widget": "text"},
      "model": {"widget": "model", "advanced": true},
      "timeout": {"advanced": true, "visible_when": {"path": ["plugin_settings", "sample", "enabled"], "equals": true}}
    },
    "actions": {
      "connection": {
        "title": "Test connection", "read_only": true,
        "input": {"type": "object", "additionalProperties": false},
        "output": {"type": "object", "properties": {"message": {"type": "string", "maxLength": 200}}, "required": ["message"], "additionalProperties": false}
      }
    }
  },
  "application": {"*": "boundary", "storage_path": "restart"}
}
```

Modes are `immediate`, `boundary`, and `restart`; the most specific relative field path
wins. Declare all owned settings, using `*` for a shared policy. Undeclared external
settings require restart. Components are `auto`, `text`, `secret`, `select`, `list`,
`connection`, `model`, and `status`; objects and lists use the shared schema editor,
`options_from` references a declared configuration collection. No plugin JavaScript is loaded.
Actions call `configuration_action(name, arguments)` on the running owner, validate input
and output, redact declared secrets and reuse operator authentication. Mutating actions
require explicit confirmation and are not included in configuration rollback.

For online updates implement `async prepare_configuration(settings)` returning the public
`ConfigurationUpdate(commit, rollback, release)` contract. Extensions receive only their
own resolved settings. Preparation must not change active state, open a device in use,
dispatch work, or invalidate credentials. Raise `ConfigurationPending` to wait for an idle
boundary. Validate allocations before returning; clean them up yourself if preparation fails.
Commit must recheck its boundary and support partial failure. Rollback must restore the
previous effective state; `release(committed)` closes obsolete resources only. Use
`context.configuration_resource(id)` to read the candidate resource view during preparation.

The host prepares owners in dependency order, suspends new request admission while switching,
rolls back in reverse order and blocks owners/dependents if recovery fails. Existing work
must retain its context and identity. The sample plugin demonstrates an atomic settings
and resource update. Device connection replacements preserve fixed main voice bindings;
changing those bindings remains a restart operation.

Bundled adapters registered for owned connection replacement may first handle a policy-only
change through their own `prepare_configuration` hook. Returning `NotImplemented` from this
internal hook delegates a connection change to the registered drain/replacement path; it is
not a fallback contract for arbitrary external extensions. The framework never edits provider
policy fields itself. Linux audio minimum volume continues to affect only its fixed main
output; extra speakers retain their existing policy.


## Operator drain hooks

Optional synchronous `maintenance_pause()` / `maintenance_resume()` hooks stop and resume
new autonomous work; they must not cancel already accepted operations. A synchronous
`maintenance_pending()` returns bounded records containing `id` (optional) and `state`,
without payloads or credentials. Include queued work, preparation, clarification, external
operations and finalization. Empty means the owner has no finite work remaining. Passive
polling/housekeeping may explicitly return an empty list if it cannot initiate business work.
External package adapters forward these hooks without exposing the host. An extension with
background workers and no pending-work contract blocks a graceful configuration restart;
silence is not evidence of completion. Operator force still follows cancellation and owned
shutdown, retaining unknown outcomes and existing no-replay semantics.

Configuration resources can be declared in the same JSON description:

```json
{"resources":[{"id":"policy","path":"policy.json","kind":"json","title":"Report policy"}]}
```

Resources are confined to `config/plugins/<plugin-id>/`, bounded, versioned and restored
with configuration. Kinds are `text`, `json`, `credential`; `sensitive: true` makes the
resource replacement-only without returning its contents. JSON may declare `schema` for
validation. Secrets should normally use declared `.env` references instead. OAuth/rotating
login stores are account operations and must not be registered as static restore resources.

Use `context.configuration_resource("policy")` to read the loaded generation; do not
reopen the mutable source path. This returns only the caller's declared resource. Optional
resources may not yet exist. `x-reference` points at another configuration object (e.g.
`model_api.entries`) for a read-only name selector, without inference or network discovery.
Schema `$ref` values must be acyclic local references. The sample package covers automatic
forms, conditional fields, a secret and a private JSON resource as well as tools/skills.

### Independent configuration management

Both presets start `core/configuration` through `app/management`; management owns no Agent
loop and exposes no model tools. A minimal independent bootstrap permits repair when business
configuration cannot parse. Owner JSON descriptions drive defaults, validation and forms.
TOML remains the sole instance source, with registered credential/MCP/skill resources handled
through versioned private transactions. Saving submits an immutable candidate to the shared
update coordinator; the effective generation advances only after a successful switch. Structural
changes wait for an explicit restart, and failed candidates are not retried automatically.
Linux guardian ownership closes escaped subprocesses after manager loss; unknown
operations never replay. External gateway deployment remains an explicit operator step.
See the [configuration guide](../../src/assistant_runtime/core/configuration/README.md).

### 运行健康信息

插件遇到使功能不可用的部署或运行问题，应通过 `context.set_health("degraded", "简短原因及修复线索")` 报告；恢复后调用 `context.set_health("ready")` 清除错误。错误不要包含凭据、用户消息或供应商原始响应。管理状态接口统一聚合，配置页持续显示；不要另建插件通知系统，也不要把捕获异常当作成功。未捕获的后台任务异常仍由框架报告。

## 受控执行集成

API 2 的可信服务插件使用 `execution_control` 注入资源；必须声明 execution 依赖并配置 owner 白名单。授权、上下文、轮次、用量、产物与 Windows 隔离范围见[受控执行接入](controlled-execution.md)。模型工具不能使用该控制句柄，不能用 work_id 代替项目授权。
