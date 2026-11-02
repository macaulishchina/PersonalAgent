# 安装与首次启动

按顺序完成：安装 → 创建实例 → 配置模型 → 开始聊天。已有实例直接阅读[日常维护](operations.md)，不要重新初始化。

## 1. 准备环境

准备 Git、uv、Python 3.12 x64、Node.js ≥22.19.0 和 npm，并确保当前账号有仓库访问权限。Windows 使用 PowerShell。

尚未安装 uv 时，使用[官方安装器](https://docs.astral.sh/uv/getting-started/installation/)：

```powershell
# Windows / Windows Server
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```sh
# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装后重新打开终端，执行 `uv --version` 确认可用。

全程使用最终运行账号，在选定的本地目录执行；Windows 使用 NTFS。新安装将仓库克隆到当前空目录 `.`，已有仓库跳过克隆命令。后续命令均在仓库根目录执行，实例放在 `$PWD/instance`，该目录已被 Git 忽略。

## 2. 安装应用

选择所在平台的一组命令，每条成功后再执行下一条。

常用安装入口会固定 Python 3.12、使用锁文件、以非 editable 方式安装，并记住成功安装时的依赖组合。首次 Windows 默认 slim、Linux 默认 standard；已有记录时沿用原选择。它只安装应用，不创建实例、不注册系统服务，也不停止或启动后台。

### Windows / Windows Server

Windows 默认使用 slim，提供 CLI 和独立配置网页；需要完整功能时也可显式使用 standard 并安装其依赖。

```powershell
git clone git@github.com:macaulishchina/PersonalAgent.git .
$env:PYTHONUTF8 = '1'
.\scripts\install.ps1
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
pa install-pi
pa init "$PWD\instance" --preset slim
pa use "$PWD\instance"
pa configuration token --management "$PWD\instance\management.toml"
```

Windows 安装 standard 时，使用 `.\scripts\install.ps1 --preset standard`，初始化时使用 `--preset standard`。安装脚本会自动构建聊天前端；设备额外依赖用 `--extra` 添加，详见[安装选项](../reference/installation.md#安装脚本选项)。

### Linux

以下安装 standard，提供 CLI、配置网页和聊天网页。

```sh
git clone git@github.com:macaulishchina/PersonalAgent.git .
bash scripts/install.sh
export PATH="$PWD/.venv/bin:$PATH"
pa install-pi
pa init "$PWD/instance" --preset standard
pa use "$PWD/instance"
pa configuration token --management "$PWD/instance/management.toml"
```

Linux 只需 CLI 时，使用 `bash scripts/install.sh --preset slim`，初始化时使用 `--preset slim`。slim 不构建聊天前端；显式添加 `--extra web` 时会构建。

最后一条命令显示配置网页的管理员口令。Pi 安装位置会自动记录，保留默认 `pi_binary = "pi"` 即可。[自定义目录与 wheel 安装](../reference/installation.md)适用于另有部署要求的环境。

上面的 PATH 设置仅对当前终端生效。以后可将应用目录下的 `.venv\Scripts`（Windows）或 `.venv/bin`（Linux）的**绝对路径**加入用户 PATH；也可直接调用其中的 `pa.exe` 或 `pa`。

## 3. 启动并配置模型

在当前终端执行，保持窗口打开：

```sh
pa run
```

1. 在服务器本机打开 `http://127.0.0.1:8047/config/`；远程浏览器使用服务器 IP，按需放行防火墙端口。
2. 用刚才读取的管理员口令登录。
3. 按[配置模型与访问](configuration.md)设置账号、模型和主对话绑定，点击“保存并生效”，确认生效后开始聊天。
4. 确认页面显示业务已启动、配置已应用。

Windows 默认使用 slim，扩展按各自的认证启用，见[模块索引](../reference/modules.md)。全部现有扩展已声明支持 Windows，standard 可显式选择，语音入口仍停用；外部插件继续独立检查认证。两种平台的配置网页都可在模型尚未配置时使用。

## 4. 开始聊天

完成配置后，在运行 `pa run` 的终端按 Ctrl+C，等待前台进程退出，再执行：

```sh
pa
```

`pa` 自动启动后台并连接聊天。发送一条消息确认模型实际可用；输入 `/exit` 离开，后台继续运行。

启用 Web 插件后，聊天地址为 http://127.0.0.1:8047/，无需 URL 后缀；配置页位于 `/config/`，共享管理端口。只需开放管理端口；聊天和管理登录保持独立。关闭 Web 不影响配置页。

## 下一步

- [CLI 使用](cli.md)：切换会话、查看历史、补充和取消。
- [日常维护](operations.md)：状态、日志、备份及升级。
- [安装系统服务](operations.md#安装系统服务)：需要开机启动时执行；日常 CLI 聊天不要求预先安装系统服务。
