# slim / standard 部署

原生 Windows 的安装、服务及待验收边界见 [平台部署与验收](platforms.md)。以下 shell
示例采用 Linux 路径；Windows 使用同一 CLI 和插件配置。

standard 实例包含网页文字/图片对话、模型接入、用量控制、后台任务和通知。
两种安装组合都交付全部核心插件及其 Python 运行依赖；通过共同的 core 依赖组合锁定。
核心插件是否运行由默认启用列表决定，不需要为了启用核心插件另装其代码或依赖。
嵌入模型权重、账号、外部 MCP 服务进程和设备仍须按需配置，不会自动下载或调用。
音频引擎、云盘客户端、搜索、家庭设备等扩展依赖仍按需安装和启用。

slim 与 standard 使用同一宿主、执行器、插件协议及 CLI；preset 仅生成初始配置。
slim 默认启用 ai_usage、models、workfiles、pi、execution、integrations，不启动网页、对话、任务调度、
通知、设备或语音对象。standard 在公共执行底座上提供网页、主对话、任务和通知。
两者都可以通过修改显式插件列表并重启调整启用状态；不提供运行中热切换。
未启用的核心插件不导入、不构造、不启动后台工作。运行时不检查 preset 名称。
integrations 在两者中默认启用，但初始外部 MCP 注册表为空，不继承个人 Coding AI 配置。

## slim 初始化

```sh
uv sync --locked --extra slim --no-default-groups --no-editable
scripts/setup_pi.sh /absolute/pi-runtime
.venv/bin/python -m assistant_runtime.app.cli init /absolute/slim-instance --preset slim
.venv/bin/python -m assistant_runtime.app.cli --config /absolute/slim-instance/config/config.toml run
```

slim 不需要聊天前端构建；两种部署均默认提供独立配置网页 `0.0.0.0:8047`。
slim 不启动聊天端口，本地执行 CLI 通过实例私有控制通道调用能力。
`executions submit|get|events|answer|supplement|cancel` 在两种组合中相同。
模型未配置时可启动和查看状态，执行提交明确报错，不寻找其他付费模型。
公共执行支持文字、登记文件／图片和单次工具／技能范围；`files put|get` 管理输入引用。
slim、standard 都包含核心图片输入所需的 Pillow，是否启用网页入口单独决定。
主对话、任务和直接执行共用执行准入与单次执行记录；任务计划、长期目标和通知归属
仍由各自模块管理。slim 按需启用 tasks／notifications 时，可用 inbox
通知渠道独立运行后台任务，不需要添加对话、设备或语音。

## standard 仓库安装

在新部署目录使用 Python 3.12、Node.js ≥22.19.0 和 npm，从锁文件安装；
以下命令不要对运行中的旧环境执行。

```sh
uv sync --locked --extra standard --no-default-groups --no-editable
npm --prefix frontend ci
npm --prefix frontend run build
scripts/setup_pi.sh /absolute/pi-runtime
.venv/bin/python -m assistant_runtime.app.cli init /absolute/instance
.venv/bin/python -m assistant_runtime.app.cli --config /absolute/instance/config/config.toml plugins
```

将实例的 `pi_binary` 填为安装脚本输出的绝对路径；模型启用前按需设置独立 workspace。
Pi 安装使用 `deploy/pi/package-lock.json`，锁定执行器及传递依赖，启动不自动安装。
原生账号库优先从配置的 Pi 可执行文件解析包位置，本地安装无需全局 npm 包。
如果使用启动包装器，则检查版本，并从已选 Node/npm 安装解析同版本 SDK。
服务安装器固定 Python 和解析后的 Node 目录，不复制开发终端的整份 PATH。
仓库安装默认复制发布代码，避免运行服务随源码编辑变化；开发时可省略 `--no-editable`。
构建缓存键覆盖源码及随包资源，修改 Python、模板、技能或前端产物后会重新构建本地包。
standard 首次前端构建后，再执行一次上述 `uv sync`，把新产物安装进应用环境。

初始化使用发布包内的模板，拒绝覆盖已有目录；配置与凭据文件保持私有权限。
standard 默认网页只监听 loopback:8046，模型未配置时显示“待配置模型”，不派发付费请求。
语音保持停用；没有声卡和音频依赖也能启动。

## 模型与发布包

选择已经验收的模型/账号、支持的思考档位和任务模型配置，填入私有凭据或完成
操作者授权，再重启启用。原生账户路线不要求 LiteLLM；配置需要协议转换的 API
路线时，才生成私有网关配置。不要复制其他应用会轮换的登录令牌。

从 wheel 安装时，先安装所选组合导出的锁定依赖，再安装同一个 wheel。包内包含初始化模板、
插件声明、技能、Pi 适配资源和前端构建产物；不依赖源码目录和个人数据。
构建前清理本次构建的 `build/`，避免 setuptools 把已删除模块混入新包。

在干净构建目录完成前端构建后，导出并一起交付锁定依赖及 wheel：

```sh
mkdir -p dist
uv export --locked --extra standard --no-default-groups --no-emit-project --output-file dist/requirements-standard.txt
uv export --locked --extra slim --no-default-groups --no-emit-project --output-file dist/requirements-slim.txt
uv build --wheel
```

在新环境安装，避免再次解析或升级依赖：

```sh
python3.12 -m venv /absolute/app-env
/absolute/app-env/bin/python -m pip install --require-hashes -r /absolute/release/requirements-standard.txt
/absolute/app-env/bin/python -m pip install --no-deps /absolute/release/personal_agent-0.2.0-py3-none-any.whl
/absolute/app-env/bin/personal-agent init /absolute/instance
```

wheel 自带 Pi 的锁文件，安装后可使用 `personal-agent install-pi /absolute/pi-runtime`；
也可预先按上面的脚本安装到明确目录。
slim 安装改用 `requirements-slim.txt`，初始化加 `--preset slim`，无需构建前端。
原生账号子进程使用收敛的环境变量，Node 本身须能直接启动，不依赖临时终端的动态库设置。

独立插件包由操作者安装到同一应用环境，通过实例配置显式启用。
`state/plugin-installation.json` 记录生效的插件版本；不支持运行时在线安装/热更新。

## 验收与现有实例

检查分为源码全量检查、发布包隔离安装、供应商/设备实测以及生产切换。
合成服务测试验证框架协议与执行行为，不代表新的供应商端点或硬件已经验收。

本次隔离安装使用固定摘要的 Python 3.12 容器、standard 锁定依赖、锁定的 Pi 安装及
合成模型服务。运行时断开外网，只测试本地 HTTP；不带生产凭据或个人数据。
覆盖网页图片输入、跨模型任务、回退额度、扩展工具、取消及重启恢复。

保留现有配置、凭据、记忆证据、媒体与数据库。更换代码目录本身不迁移或恢复旧任务。
日常开发在当前项目原位进行；生产运行安装后的发布包，不随源码修改变化。
生产切换需另行核对在途操作、备份、更新服务入口及实例配置，再执行上线检查。
不要用最小模板覆盖已有实例。

### 旧实例显式声明

本次取消了宿主隐式加入插件的行为。旧助手实例在独立配置副本中应补齐
`ai_usage`、`workfiles`、`devices`、`assistant`、`conversation`，并按需加入
`execution`。保留原有模型、工具、设备及任务配置；不要用 preset 覆盖它们。
缺少依赖会在资源构造前明确报错。语音仍停用，`voice_disabled` 仅供现有
standard 交互状态使用，不属于 slim。

对旧助手配置使用一次性候选生成命令：

```sh
personal-agent --config /absolute/old/config/config.toml prepare-config /absolute/candidate-config
```

它显式加入对话、公共执行及所需依赖，保留其它配置值、TOML 分片与原实例绝对 root；
候选目录内 `original/` 留存原文件副本。命令不启动插件、不请求模型、不修改源文件、
数据库或服务，也不覆盖已有候选目录。输出列出新增插件，便于审查。它面向以前依赖
隐式助手装配的配置，不应拿来把新建的 slim 自动扩成 standard。审核后，生产替换另行安排。

## 系统配置管理

`run` 默认启动独立管理进程及其业务进程；服务安装器使用同一管理入口。
业务重启不关闭配置页面；模型待配置、业务配置损坏或可选插件失败时仍可管理。

```sh
personal-agent configuration token --management /absolute/instance/management.toml
personal-agent configuration reset-token --management /absolute/instance/management.toml
```

在 8047 端口使用独立管理员口令登录。编辑保存只修改文件，确认应用才会等待或
终止当前工作并重启。普通配置、静态凭据和已选技能使用同一文件版本；OAuth 登录、
退出是单独确认的即时操作。备份、审计及回滚日志位于实例私有的 `state/configuration/`。
外部网关的部署步骤不能通过业务重启代替，页面会保留待处理标记。

`management.toml` 是独立启动配置，不能在线迁移 root/entry。管理监听地址变化会在
应用时尝试新地址，失败保留旧入口。若已启用的 Apple Home receiver 也使用 8047，
需显式选择另一个管理端口；校验会报告冲突，不擅自改变家庭设备配置。
代理来源、文件修复和恢复边界详见 [配置模块说明](../src/assistant_runtime/core/configuration/README.md)。

现有生产安装不会因修改安装器或运行自动测试而切换。部署时需按既有程序先核对在途
工作、停服备份，再更新单个服务入口；不要同时运行新旧两个业务实例。
