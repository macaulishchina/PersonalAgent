# 插件开发规范

## 平台基础设施

`platforms/` 集中提供文件锁、私有文件、目录句柄、进程所有权、本地 IPC 和服务集成。
公共契约位于 `api/system.py`，包启动时只绑定惰性后端；Portalocker、pywin32 等依赖
在使用时加载。内核不导入平台实现。应用和执行适配器负责装配，事务恢复、执行状态、
额度及重试仍由各自插件拥有，平台层不得实现业务策略或第二套 Agent 循环。

独立插件声明 `resources=("system_io",)`，通过 `context.resource("system_io")` 使用
公开设施；该资源不提供宿主、配置全集或插件注册表。文件须位于插件自己的私有目录
或已授权工作资源中。文件锁持有期等于打开文件的生命周期；创建凭据应使用
`atomic_write`，不能认为 Windows 的 `chmod(0600)` 已实现隔离。

扩展插件通过静态 `platform.json` 的 `supported_platforms` 声明平台认证，运行时声明
从同一文件读取。单文件插件使用 `<模块名>.platform.json`。当前仓库全部扩展只认证
Linux，包括 `web`、`voice_disabled`；内核范围及版本不变。`windows` 同时表示普通
Windows 和 Windows Server。认证是随发布维护的支持声明，不是签名或在线认证服务。
独立包必须在 entry point 模块对应路径打包上述元数据；缺失、无效或与运行时声明不一致
均不能启用，不再对扩展自动假定跨平台支持。管理页无需导入扩展代码即可展示认证。
配置保存和启动装配都检查当前平台，未认证时拒绝启用。Windows 默认使用 slim，
显式选择 standard 会在创建实例目录前拒绝。已有显式配置不会自动删改。
Windows 适配完成度和认证启用策略分开记录，
详见 [平台部署与验收](docs/platforms.md)。

公共接口为 `assistant_runtime.api`：`PluginDefinition`、`Manifest`、
`PluginContext`、`CallIdentity` 和 `Tool`。仓库内和独立包采用同一声明模型。

## 声明与生命周期

每个仓库内插件的 `declaration.py` 拥有配置校验、依赖、工厂、能力、资源、
技能和工具服务声明。框架目录只登记内置插件的身份与模块位置，不重复登记工具名。
独立包通过 `personal_agent.plugins` Python entry point 声明，名称与 manifest 一致。
只导入显式启用的独立包，不自动安装或更新。外部包不能占用内置身份、声明关键插件，
或使用核心内部工厂。

`Manifest` 包含插件版本、框架接口版本、必需/可选依赖及支持的依赖主版本。
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
详细操作和验收范围见 `core/execution/README.md`。

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
迁移和重启恢复禁止重放未知操作。详见 `core/conversation/README.md` 与 `core/cli/README.md`。

## 工具、技能和安装记录

工具需包含名称、说明、输入/输出 JSON Schema、处理入口和独立的读写、破坏性、
幂等性、外部访问属性。这些属性不代替权限校验，也不等于费用许可。
内置工具的唯一声明在所有者的 `tools.py`；MCP 和直接业务入口共用能力处理器。
独立插件返回 `Tool` 声明；直接工具调用和动态 MCP 投影使用同一注册表及 Schema。
Pi 主会话和后台任务自动获得动态扩展工具服务，不需要改中央工具名单。

技能随所属插件打包，由其声明选择；禁用插件不加载技能。技能重名或实例覆盖会报错。
`.agent`、`.agents` 与根目录开发指南不作为助手技能加载。

`pa plugins` 列出启用插件、类型、版本和依赖；启动时私下记录
`state/plugin-installation.json`。配置修改重启生效，安装/升级由操作者显式执行。
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
Both presets include the independent operator console; see `core/configuration/README.md`.

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
through versioned private transactions. Saved files do not change a running worker's frozen
generation. The management process verifies readiness and version after explicit drain/force
application. Linux guardian ownership closes escaped subprocesses after manager loss; unknown
operations never replay. External gateway deployment remains an explicit operator step.
See the [configuration guide](src/assistant_runtime/core/configuration/README.md).
