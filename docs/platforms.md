# Linux／Windows 正式部署指南

本文是从仓库安装、配置并长期运行 Personal Agent 的完整流程。按所在系统执行安装段落，
然后依次完成配置、前台启动和服务安装。全部使用正式目录；不另建测试安装，不把测试目录
作为服务依赖。可选的临时平台验收放在文末，不属于部署必做步骤。

Linux 已有运行与回归记录。Windows 11 x64、Server 2022／2025 x64 的适配已加入，
但实机验收尚未完成；本文提供其正式安装流程，不代表这些系统已通过生产验收。
语音仍停用；Windows 不支持现有 PipeWire、V4L2 和 Linux HID 插件。

## 1. 确定安装组合、账号和目录

- **standard**：网页文字／图片对话、执行、模型、用量保护、后台任务、通知及外部 MCP 接入。
- **slim**：执行、模型、用量保护、文件及外部 MCP 接入；默认没有聊天网页、任务调度和通知。
- 两种组合都有独立系统配置网页和同一套 CLI。这里以 standard 为主；选择 slim 时，
  仅将安装和初始化命令中的 `standard` 改为 `slim`，并省略聊天前端构建。

| 用途 | Windows | Linux |
|---|---|---|
| 应用仓库及 Python `.venv` | `C:\PersonalAgent\app` | `$HOME/PersonalAgent/app` |
| 固定版本 Pi 及 Node 依赖 | `C:\PersonalAgent\pi` | `$HOME/PersonalAgent/pi` |
| 配置、凭据、状态与业务数据 | `C:\PersonalAgent\instance` | `$HOME/PersonalAgent/instance` |

三个目录都长期保留。服务引用 `app` 中的 Python，配置引用 `pi` 中的执行器；
`instance` 必须备份。安装、启动和验收命令不会自动删除这些目录。

从开始就使用最终运行账号。Windows 服务安装要求此账号与实例拥有者一致，并具备
“作为服务登录”权限；不能使用 LocalSystem。Linux 使用普通账号的 systemd 用户服务。
实例状态、SQLite、锁和凭据必须放在本机文件系统，Windows 使用 NTFS，不使用网络盘。

本流程用于**全新实例**。已有实例不要重新 `init`，也不要用模板覆盖其配置；升级见第 7 节。

## 2. 安装应用和 Pi

准备 Git、Python 3.12 x64、uv、Node.js 和 npm。当前锁定 Pi 要求 Node.js ≥22.19.0。
确认它们在最终运行账号下可用，Node 安装目录长期保留。Windows 使用原生 PowerShell，
不需要 WSL 或 Git Bash。只有显式选择 Bash 工具时才另装 Git Bash。

以下每条命令成功后再执行下一条；出现错误时先处理，不继续初始化或安装服务。
仓库需要认证时使用自己的 Git 凭据，不把令牌写进克隆地址。

### Windows：PowerShell

```powershell
New-Item -ItemType Directory -Force C:\PersonalAgent | Out-Null
git clone https://github.com/macaulishchina/PersonalAgent.git C:\PersonalAgent\app
Set-Location C:\PersonalAgent\app
$env:PYTHONUTF8 = '1'
uv sync --locked --python 3.12 --extra standard --no-default-groups --no-editable
npm --prefix frontend ci
npm --prefix frontend run build
uv sync --locked --python 3.12 --extra standard --no-default-groups --no-editable
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli install-pi C:\PersonalAgent\pi
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli init C:\PersonalAgent\instance --preset standard
```

### Linux：Shell

```sh
mkdir -p "$HOME/PersonalAgent"
git clone https://github.com/macaulishchina/PersonalAgent.git "$HOME/PersonalAgent/app"
cd "$HOME/PersonalAgent/app"
uv sync --locked --python 3.12 --extra standard --no-default-groups --no-editable
npm --prefix frontend ci
npm --prefix frontend run build
uv sync --locked --python 3.12 --extra standard --no-default-groups --no-editable
.venv/bin/python -m assistant_runtime.app.cli install-pi "$HOME/PersonalAgent/pi"
.venv/bin/python -m assistant_runtime.app.cli init "$HOME/PersonalAgent/instance" --preset standard
```

第二次 `uv sync` 将构建后的聊天前端资源装入应用包。slim 省略两条 npm 命令及第二次
同步即可。Pi 安装使用仓库随包交付的 npm 锁文件；不另行全局安装最新版 Pi。
记录 `git rev-parse HEAD` 输出，作为本次部署版本。
若 CLI 提示没有 `install-pi` 或 `service` 子命令，先确认使用的是本目录的 `.venv`，
且锁定安装已经成功完成；仅拉取源码不会更新此前安装的应用包。

## 3. 设置执行器和访问地址

编辑正式实例的 `config/config.toml`，将已有的 `pi_binary` 改为安装命令输出的绝对路径。
Windows 对应：

```toml
pi_binary = 'C:\PersonalAgent\pi\node_modules\.bin\pi.cmd'
pi_shell = "auto"
```

Linux 填写实际绝对路径，例如安装命令输出的 `/home/你的账号/PersonalAgent/pi/node_modules/.bin/pi`；
**TOML 中不展开 `$HOME`**。`pi_shell = "auto"` 在 Linux 选择 Bash、Windows 选择 PowerShell。
不要重复添加已经存在的字段。

| 入口 | 初始地址 | 配置位置 | 登录口令 |
|---|---|---|---|
| 系统配置网页，两种组合都有 | `0.0.0.0:8047` | `instance/management.toml` 的 `host`、`port` | 独立管理员口令 |
| 聊天网页，仅 standard 默认启用 | `127.0.0.1:8046` | `instance/config/services.toml` 的 `[web]` | 独立聊天口令 |

本机浏览器使用 `http://127.0.0.1:8047` 和 `http://127.0.0.1:8046`。
局域网使用服务器实际 IP；如需局域网聊天，将 `[web].host` 改为 `"0.0.0.0"`。
主机防火墙按需放行所用端口，安装器不会自动修改防火墙。管理与聊天账号互不通用。
公网访问另行配置 HTTPS 反向代理及受信来源，不直接套用局域网访问方式。

## 4. 首次启动、登录和配置模型

在应用目录启动前台进程，暂时保持终端打开。

Windows：

```powershell
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\PersonalAgent\instance\config\config.toml run
```

Linux：

```sh
.venv/bin/python -m assistant_runtime.app.cli --config "$HOME/PersonalAgent/instance/config/config.toml" run
```

`run` 启动配置管理进程及业务进程。初始模型未配置时允许管理页面工作，执行返回待配置，
不会自动选择收费供应商。另开同账号终端，进入应用目录，读取管理员初始口令：

Windows：

```powershell
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli configuration token --management C:\PersonalAgent\instance\management.toml
```

Linux：

```sh
.venv/bin/python -m assistant_runtime.app.cli configuration token --management "$HOME/PersonalAgent/instance/management.toml"
```

登录系统配置网页后，可在页面修改管理员密码。忘记密码时，将上述命令的 `token` 换成
`reset-token`，在本机重置。不要把口令放进聊天、截图或日志报告。

### 配置模型接入

在“模型与账号”登记账号、供应商及模型别名，再设置执行用途绑定。配置保存在实例的
`config/models.toml` 等已登记文件；页面保存后仍需“应用”才会重启业务、加载新版本。

1. **原生账号路线**：选择 Pi 支持的供应商及 OAuth／API Key 方式，供应商引用该账号。
   ChatGPT 使用独立 OAuth 授权，账户输出限制选择 `model`；不复制 Codex 的登录文件。
   这条路线不需要 LiteLLM，也不需要 Docker。
2. **模型条目**：设置稳定别名、供应商、实际模型 ID、可用思考档位及用途能力。
   原生账号可查询模型目录，但目录存在不代表所有能力已经验收。主对话的 `main_model`
   引用这里的别名，`main_reasoning_effort` 必须是该模型支持的档位；任务档位也引用模型别名。
3. **启用与检查**：完成账号授权或私有密钥录入，启用所需供应商及 `model_api.enabled`，
   检查用途绑定后保存、应用。不要直接启用未配置的其他供应商或回退项。
4. **协议网关路线**：若使用 `preset`／自定义 URL 的 API 供应商，按
   [模型接入说明](../src/assistant_runtime/core/models/README.md)生成并部署独立网关。
   重启助手不会更新网关容器；页面上的待部署步骤完成后才算此路线可用。

OAuth 可使用页面入口；本机 CLI 也提供 `model-accounts login --account 账号名`，
账号名必须先在配置中声明。跨机器登录时按终端提示转发本地回调端口，不把回调地址发到聊天。
密钥使用页面的私有凭据入口或实例 `.env`，不写入仓库和普通 TOML。
供应商及能力的具体字段、授权限制见[模型模块说明](../src/assistant_runtime/core/models/README.md)。

### 确认业务可用

standard 的聊天口令默认位于 `instance/web-access-token`，在业务首次成功启动后生成。
用同账号在本机读取该文件，登录聊天网页；它与配置管理员口令不同。若修改过
`web.token_file`，以配置中的文件为准。

确认管理页面显示业务已启动、配置版本已应用，无需要处理的依赖或网关错误。
standard 再确认聊天页可以加载；slim 使用 CLI 查询运行状态。两种组合的查询命令是：

Windows：

```powershell
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\PersonalAgent\instance\config\config.toml status
```

Linux：

```sh
.venv/bin/python -m assistant_runtime.app.cli --config "$HOME/PersonalAgent/instance/config/config.toml" status
```

这些状态检查不调用模型。模型可用性由操作者在完成授权后，通过实际使用确认；不要把
“服务已启动”当作供应商实测通过。slim 的执行提交、过程、回答和取消接口见
[公共执行说明](../src/assistant_runtime/core/execution/README.md)。

## 5. 安装为系统服务

先在前台终端按 Ctrl+C，等待退出，再安装、启动服务。不要让前台与服务同时占用同一实例。
以下命令均在应用目录执行。

### Windows SCM

使用实例拥有者账号打开提升权限的 PowerShell。此账号必须有实际密码及“作为服务登录”权限，
不能只依赖 Windows Hello PIN。安装时会私下询问密码并验证服务登录权限；密码不写入项目配置。

```powershell
$ServiceAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\PersonalAgent\instance\config\config.toml service install --account $ServiceAccount
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\PersonalAgent\instance\config\config.toml service start
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\PersonalAgent\instance\config\config.toml service status
```

安装设置开机自动启动，但不立即启动，因此上面单独执行 `start`。权限校验失败时先修正
账号或本地／域策略；不要改用其他账号复制凭据绕过检查。服务固定引用本次 Python、Node
路径和实例目录，之后不能删除这些运行依赖。

### Linux systemd 用户服务

```sh
.venv/bin/python -m assistant_runtime.app.cli --config "$HOME/PersonalAgent/instance/config/config.toml" service install
.venv/bin/python -m assistant_runtime.app.cli --config "$HOME/PersonalAgent/instance/config/config.toml" service start
.venv/bin/python -m assistant_runtime.app.cli --config "$HOME/PersonalAgent/instance/config/config.toml" service status
```

如需开机自动运行且注销后继续运行，由管理员为该运行账号开启 linger：

```sh
sudo loginctl enable-linger "$(id -un)"
```

安装命令已经启用用户服务。不要使用 `sudo` 初始化实例或运行上述应用命令，以免改变文件归属。

## 6. 上线确认和日常操作

服务启动后重新检查配置页面、运行状态和 standard 聊天页面。Windows 首次部署还需实际
确认注销后运行及系统重启后启动；Windows 实机结果尚未验证，遇到失败应保留错误信息。

两种系统使用同一 `service` 子命令。在第 5 节命令中按需替换最后的动作：

| 动作 | 用途 |
|---|---|
| `status` | 查询系统服务状态；业务是否正常还需看配置网页和运行 `status` |
| `logs` | Linux 查看用户服务日志；Windows 查看实例业务日志，SCM 启动故障另看 Windows 事件查看器 |
| `stop` | 停止管理及业务进程，等待所属进程收尾 |
| `start` | 启动已安装服务 |
| `restart` | 重启整个服务，配置网页会暂时断开 |
| `uninstall` | 停服后移除服务登记；不删除应用、Pi、配置、凭据或数据 |

普通配置调整使用网页“保存 → 应用”，可选择等待当前工作结束或确认终止后重启业务。
这不等同于重启整个系统服务。未知外部操作不会因为服务重启自动重做。

## 7. 备份与升级

升级前核对在途工作并完成收尾，停止服务后再更新依赖。不要在服务运行中执行 `uv sync`、
重新安装 Pi 或移动应用路径。

1. 记录当前 Git 提交，备份整个正式 `instance`（含 `.env`、配置、OAuth 凭据、数据库和业务文件）。
   备份放在独立私有位置，保留原有访问限制；不要只复制 TOML，也不要提交 Git。
2. 获取目标发布提交并阅读变更说明。保留实例，**不重新执行 `init`**。
3. 在原应用目录按第 2 节重新安装锁定依赖、构建前端并安装包。Pi 锁定版本变化时，
   用同一 `install-pi` 命令更新原正式 Pi 目录。
4. 按版本说明执行必要的显式配置转换；不要用默认模板替换已有配置。安装路径或服务入口
   变化时需重新安装服务登记，不能只修改模型配置。
5. 启动服务并重复第 6 节检查。失败时先停止新进程并检查日志；不要反复重启未知执行。
   需要回退时按备份及版本变更说明处理，不假定旧代码能读取升级后的数据结构。

本文的正式目录不参与自动清理。卸载或迁移时先停止服务、保留数据，再逐项处理目录，
不把“删除验收文件”当作卸载方式。

## 附录：可选的临时平台验收

这一步用于排查新平台启动问题，可以跳过。使用已经安装好的应用 Python，**不再安装
测试环境或测试 Pi**。在应用目录运行所在系统对应的一条命令：

Windows：

```powershell
.\.venv\Scripts\python.exe scripts/verify_native_deployment.py --installed
```

Linux：

```sh
.venv/bin/python scripts/verify_native_deployment.py --installed
```

脚本在系统临时目录创建随机实例，使用回环随机端口，覆盖 slim／standard 的配置网页鉴权、
业务启动、管理进程异常退出及重新启动。正常退出或捕获异常时自动清理；强制杀死验收脚本
可能留下临时文件，按报错中实际路径核对后处理，不删除正式目录。

它不读取正式账号、不调用供应商、不操作媒体或设备、不安装服务，也不修改正式配置。
它不等于完整 Windows 验收：真实 Pi 工具执行、ACL／文件锁专项、SCM 安装、注销／开机启动、
配置恢复及进程树收尾仍需在目标 Windows 上核验。工程专项由仓库的
`Native platform acceptance` 工作流及测试套件承担，不是正式部署的额外安装步骤。
