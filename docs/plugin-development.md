# 插件开发规范

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
由宿主绑定，不允许在请求参数中自行指定。当前支持文字输入；附件登记与独立
slim 宿主仍在迁移中，不能据此认定两套部署预设已经完成。
详细操作和验收范围见 `core/execution/README.md`。

## 工具、技能和安装记录

工具需包含名称、说明、输入/输出 JSON Schema、处理入口和独立的读写、破坏性、
幂等性、外部访问属性。这些属性不代替权限校验，也不等于费用许可。
内置工具的唯一声明在所有者的 `tools.py`；MCP 和直接业务入口共用能力处理器。
独立插件返回 `Tool` 声明；直接工具调用和动态 MCP 投影使用同一注册表及 Schema。
Pi 主会话和后台任务自动获得动态扩展工具服务，不需要改中央工具名单。

技能随所属插件打包，由其声明选择；禁用插件不加载技能。技能重名或实例覆盖会报错。
`.agent`、`.agents` 与根目录开发指南不作为助手技能加载。

`personal-agent plugins` 列出启用插件、类型、版本和依赖；启动时私下记录
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
