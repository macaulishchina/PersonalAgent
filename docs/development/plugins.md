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

## 工具失败与执行证据

公共 `assistant_runtime.api.ToolOutcome` 描述 code、stage、dispatch、outcome、message，
以及可选 field、next_action、receipt。`ToolFailure` 用于抛出所有者可以证明的结果；
未知异常不能凭文案转换为“没有执行”。确认参数无效且尚未操作时用
`assistant_runtime.api.tool_calls.invalid_arguments(message, field)`。
直接调用与内部 MCP 保留同一结构；MCP 使用标准 content/structuredContent/isError。

操作返回后输出 Schema 不匹配时，注册表保留可序列化的 received_result，标记结果未知，
不得通过重新调用修复展示。插件应返回稳定操作 ID、真实状态及查询方式；submitted
不等于 completed。取消、重启和超时不能代替回执核查。并发资源锁和业务幂等由插件负责。
工具例子必须通过输入 Schema；有意支持开放 JSON 的字段须明确声明并解释语义。
嵌套结构不要只写 dict 而把必填项藏在提示词中。任务模块的共享契约可作为示例。

Pi 可调用性与模型可见性分离。定义被淘汰后工具仍可调用，按需查询以取得最新参数；
不能依赖历史 Schema、猜测名称或使用工具搜索放大权限。第三方 Schema 原样使用，
第三方读写提示不构成授权、自动重试或并发安全证明。配置和验收边界见
[Pi 工具机制](../../src/assistant_runtime/core/pi/README.md#统一工具调用边界)。

### 工具适配要求与检查边界

以下要求适用于新工具与本次修改的已有工具。核心插件可以使用声明的内部依赖，
但对模型暴露的工具仍遵守同一参数与结果契约。普通插件不需要自行实现搜索、
可见定义淘汰、重复调用阻断或另一套 MCP 服务代理。

| 项目 | 插件负责 | 框架检查／运行行为 |
| --- | --- | --- |
| 参数 | 一份输入 Schema；嵌套对象、必填、枚举、条件清楚；开放字段注明用途 | 校验 Schema 和调用参数；错误不执行处理器 |
| 参数示例 | 有参数工具建议在输入 Schema 根节点的 `examples` 提供有效对象，不填凭据或真实用户内容 | 开发检查校验示例；运行时告警并去除无效示例，保留有效示例及调用能力；不会执行示例 |
| 读写属性 | 准确填写 read_only、destructive、idempotent、external，不用字符串代替布尔值 | 缺省／异常提示保守处理，使用顺序执行，不据此增加权限；无 callable 入口仍拒绝 |
| 动态约束 | 当前配置、资源或版本检查由所有者完成；操作前失败给出字段及修正条件 | `ToolFailure` 经直接调用、IPC 和 MCP 保留，不从异常文案猜测是否执行 |
| 返回结果 | 返回符合输出 Schema 的可序列化结果；异步提交返回 ID 和查询入口 | 结果格式异常保留已收到证据，标记未知，不重做动作 |
| 幂等和并发 | 幂等声明覆盖事件及其他副作用；请求 ID 冲突、资源锁和版本检查由所有者处理 | 读写提示不是自动重试许可；取消与未知结果不会自动重放 |
| 技能与说明 | 引导发现真实工具、分页读结果及查询回执；不另写一套参数和授权规则 | 启用时按插件声明装配，不加载开发工具技能 |

登记后，框架保存 Schema 的独立快照；描述接口返回副本。不得通过修改原始字典
悄悄改变运行中的定义，定义变化应通过插件正常重新装配完成。动态配置限制仍应
在业务入口检查，并给出明确错误。旧插件不必添加 `examples` 才能启用；采用
`ToolFailure` 等 `0.4.6` 新能力的版本应声明真实最低核心版本，不能为了加载而虚报范围。

插件自己的契约测试至少覆盖有效读写、无效参数无副作用、重复请求、取消／未知结果、
结果读取失败以及资源清理中实际涉及的情况；不得为了测试而调用真实付费服务或设备。
共享框架测试覆盖声明快照、示例校验、身份绑定、IPC/MCP 错误保留及原生 Pi 调用链。
内置 MCP 工具工厂统一执行静态契约回归，列举工具时检查名称唯一、说明、输入／输出
Schema、读写标记和已声明示例；测试客户端不调用业务处理器。纯推理可以声明为只读，
计量／审计回执本身不当作业务写操作；费用准入、幂等性和权限分别处理。
只有真实供应商或设备验收才能证明外部能力有效，Schema 校验不等于业务验收。

### 开发严格、运行兼容

规范用于提高新插件质量，不把所有质量问题都变成使用门槛。内置插件、示例及新插件
的开发测试应保持严格；运行时可以兼容第三方或较旧工具不完整的辅助声明，且保留
告警以便维护者修正。无需创建第二套插件格式或逐插件兼容名单。

- 说明缺失时使用工具名；坏示例只从模型可见声明中去除，不修改所有者原字典或业务规则。
- 未声明 read_only 默认按非只读工具处理；异常提示采用 read_only=false、destructive=true、
  idempotent=false、external=true。只读与破坏性冲突时按非只读处理，调用权限保持原来源。
- 第三方 MCP 不必采用框架的 ToolFailure、完整输出 Schema 或读写标记，继续使用标准
  MCP content／structuredContent／isError；提示不完整时保守顺序执行，不能伪造操作完成证据。
- 参数是否合法由已发布契约和业务所有者共同决定；明确开放的 JSON 字段照常传递。
  不猜测缺失业务参数，不擅自转换数字／布尔字符串或改名映射；需要这种转换时由该工具
  所有者或其明确的协议适配器实现并测试。
- 重名无法确定目标、没有处理入口、无法解释影响校验的 Schema、权限或身份不符仍须
  拒绝。执行后的格式问题保留原始结果／回执供读取，不得丢弃证据或自动重做。

质量告警仅记录所有者、工具名和问题代码，不记录示例值、调用参数或凭据。兼容行为
使用共同注册入口，因此直接调用和 MCP 调用保持一致。

## 内置插件的统一适配

`provides` 列举所属模块注册的公共请求，`uses` 列举消费的公共请求；`requires/optional`
描述启动和关闭顺序。运行后才发生的可选协作也应写入 `uses`，但不能为了列齐请求而
添加会造成循环的启动依赖。缺少可选能力时返回明确不可用或采用该业务已定义的路径。
`test_plugin_standards` 对全部内置所有者的声明和实际请求使用作交叉检查。

设备资源由应用先装配，适配器负责注册，设备核心再完成启动检查。适配器使用设备
资源要求启用 `devices`，属于资源可用性要求，不添加反向启动边；资源提供者缺失时
在构造前给出明确诊断。关闭继续由依赖反序和上下文资源清理完成。

| 核心插件 | 统一职责和契约 |
| --- | --- |
| ai_usage | 用量请求、持久准入与共享工作额度；条件协作明确声明 |
| models | 无工具推理、模型请求与回执；初始化失败报告降级 |
| pi | 唯一执行循环、工具发现与原生调用；不增加业务工具代理 |
| execution | 单次执行请求、取消、结果与证据 |
| workfiles | 已登记文件请求、产物与生命周期 |
| conversation | 公共对话及结果回传请求 |
| cli | 通过公共对话请求工作，不另起对话真相来源 |
| memory | 记忆请求及模型消费声明；共享读写标记与健康状态接口 |
| tasks | 任务请求、嵌套参数契约、字段补丁与执行快照 |
| notifications | 通知及关联请求；发布幂等性、优先级与投递回执 |
| events | 事件请求、记录结构与明确开放的数据字段 |
| integrations | 连接管理请求、传输结构和版本化配置补丁 |
| devices | 设备查询、操作查询／取消及音视频请求；由实际适配器决定动态启动依赖 |
| assistant | 控制、触发和语音建议请求；语音仍待独立验收 |

| 扩展插件 | 统一职责和契约 |
| --- | --- |
| web | 声明公共对话、任务、通知消费；通过入口与 HTTP 资源工作 |
| media | 媒体请求；变更、剧集定义和批次共享所属 Schema；动作仍由实际后端校验 |
| search | 搜索请求、外部只读提示及独立的用量准入 |
| decisions | 判断请求；有界候选、开放证据、可选判断失效后继续原流程 |
| observations | 观察源请求；受信提供者配置与事件写入分开管理 |
| hid_buttons | 精确硬件绑定；配置 Schema、默认值及校验归本插件 |
| pipewire | 本地音频实现选择；只获得声明的设备与音频资源 |
| airplay | 远端音响适配及所有者绑定资源注册 |
| apple_home | HomeKit 传感器适配、所属配置和资源清理 |
| go2rtc | 摄像头、录像和控制资源注册；保留稳定设备身份 |
| v4l2 | 本地视频实现选择及设备资源注册 |
| keyword | 本地识别模型资源；不因规范整理而启用语音 |
| voice_disabled | 明确不可用的语音占位能力 |
| gesture | 显式模拟触发；通过声明的触发请求调用 |

复杂输入的结构归所属插件的 `tool_contracts.py`；第一方 MCP 适配与直接业务入口可复用
`mcp/contracts.py` 的校验与字段错误映射，不能跨插件导入业务实现。独立 Python 插件
继续只声明公共 `Tool` Schema，由同一注册入口校验；第三方 MCP 无须采用第一方实现方式。
无配置、无工具或无技能的插件不必制造空文件。静态声明、所有者能力、资源依赖、
配置字段归属和技能文件都有自动检查；不会以风格统一为理由修改已有业务数据或授权。

### 工具图片的统一交付

工具应返回标准 MCP 图片块（`type=image`、`data`、`mimeType`），描述保持简短。
框架也识别已有 JSON 结果包里的图片；不要求所有第三方立即重写，但任意路径或
URL 不会被当作待保存图片。插件不应要求模型再调用一次截图或导出，才能显示
同一张已经返回的图片。写入用户指定目的地仍属于单独操作。

图片留存由 workfiles 提供，执行侧会在模型继续前收到附件回执。
`details["personal_agent.tool_media"]` 是框架保留字段，工具不能自行宣称交付成功。
回执中的 `ready/partial/failed` 与业务 `ToolOutcome/isError` 相互独立：磁盘、格式或
解码依赖导致的附件失败不能抹掉业务结果，不能据此重试原操作。
普通主对话、后台任务及 slim 共用这一路径，权限仍按原有策略约束；受控执行不绕过
其资源 broker。既有长文本截断规则不在此约定中重写。


### 结果交付与插件兼容

工具按现有声明返回标准 content/structuredContent/isError 即可，无需实现第二套文件
保存、摘要或分页服务。模型适配器自动保留大结果与可识别图片，返回稳定结果引用。
可选 `tool_outcome` 仍属于插件确认的业务证据；交付的 ready/partial/failed 仅描述
留存，不能用它推断外部操作成功或失败。第三方缺少额外元数据时保持保守适配。

内部能力读取使用公共 `FileRequest`，声明 `uses=(FileRequest,)`；不要访问结果库
或拼接工作目录。`result_read` 的版本、位置、结束标志允许恢复中断读取，不能靠
再次调用原工具恢复内容。大文本、尾部错误、原图与报告必须测试；敏感输入不进入
新增诊断日志。主对话、任务、slim 共用同一执行和交付路径，不新增 Agent 循环。
配置、保留与清理语义见 [workfiles 说明](../../src/assistant_runtime/core/workfiles/README.md)。

工具可在 details 的 `personal_agent.presentation` 提供可选 `summary`（不超过当前
模型正文预算的字符串）和 `upstream_complete`（布尔值）。有效摘要替代确定性节选，
原内容仍完整保存；缺省或无效摘要回到节选，不阻止调用。此元数据不授予权限，
不改变业务状态，不触发额外模型调用。

### Known failures and read recovery

Owners convert known business errors to public ToolFailure at their capability boundary.
Preserve the stable error code, safe message, dispatch/outcome evidence and next_action
through IPC/MCP; `retryable=True` is an explicit owner's transient-error classification,
`False` requires correction, and omission leaves classification unknown. Do not inspect
exception strings to claim that a write did not happen. Raw provider responses and secrets
remain private. Internal business workers may retain their own exception types.

Host-owned MCP read declarations can enable bounded, model-requested rereads. This is not
a permission grant or a framework retry loop; third-party MCP annotations alone are not
trusted. Explicit owner uncertainty overrides read hints. New read tools must cover both
corrected-input recovery and attempts exhaustion; writes must retain lost-reply/no-replay
tests. Tool UI projections show the safe owner explanation and exact error code.

### Optional operation results (0.4.31)

An owner with asynchronous business operations may declare `operation_request` using
one of its `provides` types. The application binds its local, read-only
`operation_outcomes` action; tasks observe its authoritative receipts without inspecting
plugin instances or tables. This is optional for API 2 plugins. Missing observations
never imply business success. Declare readiness checks with `PluginContext.available`
only for capabilities in `uses`; optional notification/task capabilities need no hard
dependency. See the [contract and media example](../guides/business-operation-results.md).
