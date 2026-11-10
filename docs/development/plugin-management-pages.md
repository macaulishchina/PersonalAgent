# 插件自主管理页面

插件完整拥有页面代码和交互；PA 提供登录、导航、草稿、统一保存和应用协调。
本规范版本为 **1**。使用 React、TypeScript、Vite 独立构建，通过 iframe 嵌入；
不共享 React 实例，不向中央前端登记组件，不运行第二套业务服务。

## 最小声明与打包

扩展插件的 `configuration.json` 使用自己的命名空间：

```json
{
  "version": 1,
  "namespace": "plugin_settings.example",
  "schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {"label": {"type": "string", "default": "我的插件"}}
  },
  "application": {"*": "restart"},
  "interface": {
    "page": {"version": 1, "root": "management", "entry": "index.html", "title": "插件设置"}
  }
}
```

`root` 相对插件包中的 `configuration.json`；`entry` 相对 root。
使用相对资源地址（Vite `base: './'`），路由使用内存路由或 hash 路由。
静态页面不能依赖 Python 业务实例已经启动。页面通过包入口点的发行元数据发现，
禁止扫描随意目录或导入未启用插件以查找页面。

侧栏仅显示当前启用草稿中、且支持当前平台的插件。声明独立配置页或正在查看该页
不使插件自动进入侧栏；未启用插件仍可从“功能与插件 → 配置”预先编辑。
切换启用状态会更新导航，跨页面草稿仍由配置外壳统一保留。

Python 包必须包含 configuration.json、plugin.json、HTML、JS、CSS 及其它页面资源。
独立包和仓库内插件使用同一声明。页面不支持远程脚本 URL、目录越界、符号链接逃逸。
仓库内插件把构建源码放在包的 `ui/`，提供 npm 锁文件与 `npm run build`。
`python scripts/build_plugin_pages.py` 按静态声明发现并构建所有内置页面，可传入插件标识只构建指定页面。
源码安装会执行此步骤。打包方须先生成页面资源，wheel 检查会发现缺失的产物。
页面资源仅供已登录管理员访问；模型工具不能加载管理员操作。

`pa plugin-ui-check` 独立校验页面版本、入口资源、命名空间和声明；不要求实例或业务启动。
`scripts/check.py` 同时执行此检查。CLI 通过不代表页面交互或插件业务已经通过验收。

## 前端 SDK 与开发模板

公共包位于 `packages/plugin-ui`，公开入口：

- `@personal-agent/plugin-ui`：TypeScript 契约、`connect()`、`mockClient()`。
- `@personal-agent/plugin-ui/react`：可选 `useConfiguration`、`AutoField`、`AutoForm`。
- `@personal-agent/plugin-ui/theme.css`：CSS 变量及基础排版、控件样式。

完整模板在 `examples/configuration-plugin`。页面可自行选择其它组件库、复杂布局和交互。
AutoForm 只是可选标量控件示例；动态对象、条件向导等交由插件编写，不定义新的整页布局语言。

```ts
const client = await connect();
const off = client.subscribe(state => render(state));
await client.patch({path: ['label'], op: 'set', value: '新名称'});
await client.patch({path: ['label'], op: 'default'});
// 页面销毁时取消自身查询/订阅；整个页面离开后 SDK 也会关闭通信。
off();
client.close();
```

所有 path 都是**相对自身配置的路径**，不能传完整实例配置路径。
框架绑定页面窗口、来源、插件和 MessageChannel，卸载后拒绝迟到编辑。
保存和刷新配置状态时，同一页面保持挂载。离开插件页面时释放 SDK 连接。
编辑只修改外层草稿，不另设保存按钮。应在控件变化时提交草稿，不等离开页面才提交。
页面切换、重新挂载不丢草稿；刷新、退出仍提示未保存修改。
草稿（包括凭据）仅在当前浏览器管理会话内，不存 localStorage/sessionStorage。

| SDK 方法 | 语义 |
|---|---|
| `state()` | 自身字段、Schema、默认值、来源、文件、已加载值、插件启用状态、配置版本、资源及保存/应用状态 |
| `subscribe(fn)` | 接收上述状态，返回取消订阅函数 |
| `patch({path,op,value})` | `set`、`create`、`default`、`replace_secret`、`clear_secret`；默认值恢复删除显式覆盖 |
| `resource(id,op,value)` | 自身声明的资源；普通文本/JSON 用 `set`，凭据用 `replace_secret/clear_secret` |
| `action(name,args,requestId?)` | 声明的即时管理操作；返回明确的完成、拒绝、不可用或待确认状态 |
| `resize(height)` | 通知内容区高度，框架限制范围 |
| `close()` | 结束本页通信及订阅，不取消已派发业务操作 |

字段提供 `loaded_value` 时表示运行实例加载的值，`value` 表示当前草稿/文件或默认值，
二者不混用。敏感字段只返回 configured 状态；输入的口令不会通过状态推送回显。
结构化 JSON 资源仍以文本写入，由资源自己的 Schema 校验。
修改时保留其它页面草稿，所有草稿由外层统一保存。完整候选、条件、依赖及业务校验
仍在服务端执行，字段错误通过 SDK 返回到插件页面。不要把浏览器校验当作唯一校验。

## 配置与应用边界

默认值和约束来自插件 ConfigurationSpec；初始化、运行时和保存使用同一解析器。
未启用但被修改的配置同样执行完整校验；缺依赖时应修复依赖，不能绕过校验保存。
未修改的旧配置不因查看页面而重写。仓库内扩展的新配置也使用 plugin_settings 命名空间。

`application` 使用既有 `immediate`、`boundary`、`restart`：

- 即时/安全边界更新由插件 `prepare_configuration` 返回 `ConfigurationUpdate`。
- 没有更新接口时由协调器决定重启，不承诺仅凭声明就能热更新。
- 重启提案必须由操作者确认；保存失败和应用失败分别显示。
- 文件事务、版本冲突、回滚和恢复继续走现有配置服务。

资源声明使用 `id/path/kind/title/sensitive/schema`。私有物理位置固定在
`config/plugins/<插件标识>/` 下，插件通过 `PluginContext.configuration_resource(id)`
读取已加载的资源版本。仓库内和独立包共用此路径机制。凭据资源 `kind: credential`
自动视为敏感，不能漏写 sensitive 后泄露内容。

## 管理操作

操作声明位于 `interface.actions`，包含 input/output JSON Schema、布尔 read_only、
title/description，以及写操作 confirmation 文案；实现 `configuration_action(name, arguments)`。
不得接受模型伪造的管理员身份。查询使用已生效配置；编辑草稿不会改变查询的运行状态。

修改性操作需要外层确认，并检查保存/加载版本，未应用配置先处理。后台在串行配置边界
内再次核对加载版本，防止确认到派发之间切换配置。页面侧与服务端都阻止进行中的重复点击。
SDK 不传管理员凭据，也不提供任意 HTTP 代理。

操作请求 ID 在发出前生成。写操作派发前记录私有回执，断连、取消等待、进程退出或结果
处理失败时返回“待确认”，**不会自动重放**。同一请求 ID 查询已记录状态不会再次执行；
跨后台重启仍保留回执。回执不保存参数、密钥或结果正文。
若需要对外部动作持续追踪，插件返回自己的 operation_id，并声明查询操作。
长耗时工作应迅速返回该引用，不长期占用管理 IPC；此框架不新增通用工作流引擎。

页面声明的管理操作由带版本的 SDK 路径派发。插件确认操作未生效时可抛出
`ConfigurationActionRejected`，携带可安全展示的错误码和原因。
普通异常仍由框架按结果待确认处理。

页面离开时停止自身轮询。已派发操作由插件生命周期管理；页面关闭不证明操作取消。
输入错误、未启用、未就绪和版本冲突在派发前拒绝。派发之后出现结果 Schema 或展示错误，
不能声称业务未执行。输出按声明脱敏；插件必须把所有敏感结果标为 writeOnly/x-sensitive。

## 信任与验收

iframe 隔离依赖和 CSS，并限制导航能力；来源检查保护页面实例通信。
**同源可信 Python 插件及其页面不是恶意代码沙箱。** 不允许安装不可信插件后宣称系统隔离。
第三方库必须随构建打包。表单由插件脚本处理 submit 事件，浏览器原生提交导航受 CSP 限制。页面 API 不授予跨插件配置编辑权。

模板验收覆盖：独立 wheel 资源、动态导航、普通/定制字段、跨页草稿、条件校验、
私有字段、JSON/文本资源、真实管理/业务进程即时更新、多步向导、业务停止后的配置访问。
契约测试补充越界、错误版本、未知结果回执、重复点击、输出校验和脱敏。
新增插件应同时保留自身业务测试，页面测试不代替供应商或硬件验收。

内置扩展统一声明管理入口。简单配置使用通用 Schema 表单；复杂交互可选择 iframe。
AI Passport 使用独立页面，演示配对、默认值继承、设备覆盖和敏感字段。聊天网页使用
`interface.page.presentation: "link"` 打开插件包里的独立管理页，同时在原位置提供表单。
静态链接页面可直接随包交付。共享设备与实例设置由 `interface.links` 指向
相应的配置栏目，配置仍在原所有者页面中编辑。
