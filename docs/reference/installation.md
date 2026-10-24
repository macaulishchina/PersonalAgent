# 安装选项

首次安装按[安装与首次启动](../guides/deployment.md)执行；本页仅说明组合、路径和发布包选项。

## slim 与 standard

| 项目 | slim | standard |
| --- | --- | --- |
| 平台 | Linux、Windows／Server | Linux |
| CLI、对话、模型、执行、用量、文件、MCP | 默认启用 | 默认启用 |
| 独立配置网页 | 默认启用 | 默认启用 |
| 个人助手、聊天网页、任务、通知 | 默认关闭 | 默认启用 |
| 语音 | 停用 | 停用 |

两种组合共享核心代码与依赖。preset 只用于生成初始配置；运行时以显式插件列表为准。模型权重、账号、外部服务和设备需要按需配置。启用扩展必须通过平台认证和依赖检查。

## 默认目录

| 内容 | Windows | Linux |
| --- | --- | --- |
| Python 环境 | 应用目录的 `.venv` | 应用目录的 `.venv` |
| Pi | `%LOCALAPPDATA%\personal-agent\pi` | `${XDG_DATA_HOME:-$HOME/.local/share}/personal-agent/pi` |
| 实例 | `pa init` 指定目录 | `pa init` 指定目录 |
| CLI 实例选择 | `%LOCALAPPDATA%\personal-agent\cli.json` | `${XDG_CONFIG_HOME:-$HOME/.config}/personal-agent/cli.json` |

程序、Python／Node／Pi 和实例都需长期保留。实例必须放在本地文件系统，Windows 使用 NTFS。实例是配置和业务数据，不是临时测试目录。

## 自定义环境位置

默认流程无需执行本节。Python 环境使用 uv 的 `UV_PROJECT_ENVIRONMENT` 指定，
必须在 `uv sync` 前设置；之后运行命令中的 `.venv` 路径也应换成该环境路径。
Python 解释器本体由 uv 管理，不复制到实例目录。

```powershell
# Windows 示例：可分别选择 Python 和 Pi 的位置
$env:UV_PROJECT_ENVIRONMENT = 'D:\AgentEnvironments\personal-agent'
uv sync --locked --python 3.12 --extra slim --no-default-groups --no-editable
& "$env:UV_PROJECT_ENVIRONMENT\Scripts\pa.exe" install-pi 'D:\AgentRuntimes\pi'
```

```sh
# Linux 示例：可分别选择 Python 和 Pi 的位置
export UV_PROJECT_ENVIRONMENT="$HOME/agent-environments/personal-agent"
uv sync --locked --python 3.12 --extra standard --no-default-groups --no-editable
"$UV_PROJECT_ENVIRONMENT/bin/pa" install-pi "$HOME/agent-runtimes/pi"
```

Pi 自定义路径也会记录在用户数据目录的 `personal-agent/pi-installation.json` 中，
无需手动配置。记录只在安装成功后更新；同一用户使用 `pi_binary = "pi"` 的实例
共享最近一次成功记录的安装。安装失败保留上一记录；记录损坏或目标被移动时明确报错，
不会悄悄换用其他 Pi。未存在安装记录时，仍可发现默认目录或已有 `PATH` 中的 Pi。

如需为不同实例固定不同的 Pi，显式将该实例已有的 `pi_binary` 改为安装输出的绝对路径；
显式路径优先于用户安装记录。**TOML 中不展开 `$HOME`**，不要重复添加字段。


## wheel 交付

发布方在干净构建目录完成 standard 前端构建后，导出两组锁定依赖和同一个 wheel：

```sh
mkdir -p dist
uv export --locked --extra standard --no-default-groups --no-emit-project --output-file dist/requirements-standard.txt
uv export --locked --extra slim --no-default-groups --no-emit-project --output-file dist/requirements-slim.txt
uv build --wheel
```

安装方在新环境使用对应依赖文件，不重新解析版本。以下为 Linux standard 示例，wheel 文件名以实际交付为准：

```sh
python3.12 -m venv /absolute/app-env
/absolute/app-env/bin/python -m pip install --require-hashes -r /absolute/release/requirements-standard.txt
/absolute/app-env/bin/python -m pip install --no-deps /absolute/release/personal_agent-0.2.0-py3-none-any.whl
/absolute/app-env/bin/pa install-pi
/absolute/app-env/bin/pa init /absolute/instance --preset standard
```

slim 改用 `requirements-slim.txt` 和 `--preset slim`；Windows 使用环境的 `Scripts\python.exe`、`Scripts\pa.exe`。包内交付初始化模板、静态声明、技能和 Pi 锁文件；standard 还需已构建的聊天前端资源。安装后继续[首次启动](../guides/deployment.md#3-启动并配置模型)。

服务固定引用安装环境和 Node，不依赖源码目录或临时开发终端 PATH。外部插件安装到同一应用环境，再通过实例配置显式启用；不支持运行时在线安装或热更新。
