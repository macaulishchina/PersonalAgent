# Linux／Windows 原生部署与验收

## 状态和边界

同一源码和 wheel 包含两套平台实现，slim／standard 的插件配置规则不变。
Windows 实机验收按操作者决定延后；代码、脚本或 Linux 测试通过，不表示 Windows
11 x64、Server 2022 x64、Server 2025 x64 已通过生产验收。

| 能力 | Linux | Windows 实现与待验收项 |
|---|---|---|
| 文件锁 | Portalocker 独占锁，关闭释放 | 同一接口；竞争、异常释放需实测 |
| 凭据／配置写入 | 私有权限、原子替换、目录 fsync | 当前账号与 SYSTEM 的受保护 ACL，文件 fsync、MoveFileEx write-through；文件占用报错并保留原件 |
| 受管进程 | 独立进程组，管理服务保留 Linux guardian | Job Object 关闭时结束后代；启动门在加入 Job 前不允许目标运行 |
| 本地控制 | Unix socket 与同 UID 检查 | 拒绝远程命名管道、ACL 和双向进程账号检查 |
| 系统服务 | 原有 systemd 用户服务 | SCM，显式指定实例账号；安装不自动启动 |
| Pi | 锁定 Pi，默认 Bash | 同一 Pi，默认原生 PowerShell；可显式指定 Bash 并安装 Git Bash |
| 通用插件 | 保持现有功能 | 模型、对话、任务、通知、媒体、搜索等共用代码，实机全量回归待完成 |
| PipeWire／V4L2／HID | 保留原实现 | 预检明确拒绝，不加载驱动依赖 |

实例状态、SQLite、凭据和锁放本机文件系统；Windows 用 NTFS，不使用 UNC／网络盘。
受管媒体目录拒绝 junction／reparse point 和保留文件名，不能用链接绕过目录边界。
Windows 没有直接等价的目录 fsync：配置替换使用 write-through，媒体文件先 fsync 后
提交并保留操作证据；不据此承诺突然断电后的目录元数据持久性与 Linux 完全等价。
异常重启保留未知结果，不自动重新执行这些操作。

## Windows 首次安装（PowerShell）

先安装 Python 3.12 x64、Node.js（满足锁定 Pi 的 engines 要求）及 uv。测试时使用
新的本地目录，切勿对正在运行的实例执行依赖同步。不需要 WSL 或 Docker；仅选择
需要协议转换的模型路线时才单独部署 LiteLLM。

在仓库目录运行：

```powershell
$env:PYTHONUTF8 = '1'
uv sync --locked --extra standard --extra dev --no-default-groups --no-editable
npm --prefix frontend ci
npm --prefix frontend run build
uv sync --locked --extra standard --extra dev --no-default-groups --no-editable
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli install-pi C:\AgentTest\pi
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli init C:\AgentTest\instance --preset standard
```

slim 将 `standard` 改成 `slim`，可省略聊天前端构建。两者都会提供配置网页。
将 `C:\AgentTest\instance\config\config.toml` 的 `pi_binary` 改为安装命令最后输出的
绝对路径。TOML 路径推荐用单引号，如 `pi_binary = 'C:\AgentTest\pi\node_modules\.bin\pi.cmd'`。
`pi_shell = "auto"` 按系统选择；`bash`／`powershell` 可显式配置，不改变全局 Pi 设置。
Windows 的 npm／npx／Pi 启动器被解析成 Node 与 JS 参数数组；其他 `.cmd/.bat/.ps1`
工具请配置真正的解释器及脚本参数，不通过拼接命令启动。

```powershell
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\AgentTest\instance\config\config.toml run
```

初始无模型账号，只提供配置／诊断，不能派发推理，不会试探收费供应商。账号通过原有
`model-accounts login` 流程配置；本轮测试不复制现有账号、对话或媒体凭据。

## Windows 服务

先验证前台运行。用**实例拥有者账号**提升权限运行安装命令；服务账号必须与初始化
实例的账号相同，避免复制 OAuth 令牌或修改他人的凭据 ACL。不能使用 LocalSystem。
账号须有“作为服务登录”权限，且没有相应拒绝策略；安装前会以服务登录类型验证，
失败时报告 Windows 错误码，不自行修改本地／域安全策略。

```powershell
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\AgentTest\instance\config\config.toml service install --account '.\AgentUser'
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\AgentTest\instance\config\config.toml service start
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\AgentTest\instance\config\config.toml service status
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\AgentTest\instance\config\config.toml service stop
.\.venv\Scripts\python.exe -m assistant_runtime.app.cli --config C:\AgentTest\instance\config\config.toml service uninstall
```

密码仅通过终端交互交给 SCM，不放配置、参数或日志。SCM 启动固定的实例 Python，
保存明确的 Node/Python PATH 与工作目录，不依赖交互式终端。`restart` 和 `logs` 同样
支持。Linux 仍使用原有 `scripts/install_service.py` 和 systemd 用户服务。

## 自动验收入口

以下检查不调用真实模型、不操作媒体账号或设备、不安装系统服务；所有实例和文件
位于独立临时目录。Windows 专项只在 Windows 运行，不以 Linux 模拟结果代替。

仓库提供可手动触发的 `Native platform acceptance` 工作流，矩阵为 Linux、Server 2022、
Server 2025，运行平台契约、真实 Pi 合成模型及两种预设启动。它未在本次工作中触发，
也不代替完整业务回归和 Windows 11／SCM 人工专项。

```powershell
# 锁、ACL、原子替换、命名管道、并发客户端、Job 后代收尾、junction 防护
.\.venv\Scripts\python.exe scripts/check.py --test test_platforms
# slim / standard：私有初始化、配置网页鉴权、业务就绪、管理进程异常退出和再启动
.\.venv\Scripts\python.exe scripts/verify_native_deployment.py
# 真实 Pi 对接本机合成模型：文本／图片、工具、澄清、用量、取消、未知结果
# 确保前面的 Pi 安装目录 node_modules\.bin 已加入当前测试终端 PATH。
.\.venv\Scripts\python.exe scripts/check.py --test test_pi_skeleton
```

`scripts/check.py` 无参数运行全量检查。全量测试还需要显式安装测试涉及的扩展依赖，
包括 media、apple-home、camera、airplay、voice；平台专用测试需按明确的平台边界
处理，不能通过大范围跳过通用测试得到“Windows 全量通过”。目前 Windows 通用测试
中的全部路径夹具、依赖安装结果及专项检查尚未实测，失败输出应保留用于修复。

Linux 使用同样的 Python 脚本，将解释器路径改为 `.venv/bin/python`。
配置网页的桌面浏览器验收仍使用 `scripts/verify_configuration_console.py`，需要另行安装
Playwright Chromium 和示例插件；不为运行应用而安装这些测试依赖。

## 发布包验收

仓库构建前先生成 standard 前端；wheel 自带插件、配置页面、前端资源及 Pi 锁文件。

```powershell
uv build --wheel
uv export --locked --extra standard --no-default-groups --no-emit-project --output-file dist/requirements-standard.txt
uv venv C:\AgentTest\wheel-env --python 3.12
uv pip install --python C:\AgentTest\wheel-env\Scripts\python.exe --require-hashes -r dist/requirements-standard.txt
uv pip install --python C:\AgentTest\wheel-env\Scripts\python.exe --no-deps dist/personal_agent-0.2.0-py3-none-any.whl
C:\AgentTest\wheel-env\Scripts\python.exe scripts/verify_native_deployment.py --installed
```

slim 单独导出其锁定依赖并在另一个全新环境重复。`--installed` 不加入源码路径；
不要使用开发环境的 PYTHONPATH 冒充 wheel 安装成功。

## 人工专项记录

Windows 11、Server 2022、Server 2025 各执行平台测试与安装检查。Server 2022 至少执行
一次完整通用回归与真实 Pi 合成服务验收。每个系统记录 OS build、Python/Node/Pi 版本、
Git 提交或未提交差异、安装方式、命令退出码和失败日志，不保存凭据正文。

还需人工验证：服务开机自动运行、注销后运行、普通停止、强制结束管理进程后没有遗留
Pi／工具子孙进程、服务账号错误、重复安装／启动、配置损坏后管理页面可修复，以及
磁盘错误／文件占用时完整配置恢复。服务停止使用配置的关闭期限，不自动无限重启失败
的业务进程。生产替换须另行安排，测试脚本不会部署生产。
