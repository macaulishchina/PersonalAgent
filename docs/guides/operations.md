# 日常维护

以下命令作用于 `pa use` 选定的实例。操作前确认选择；临时指定实例使用 `pa --config <配置文件> <命令>`。

## 查看、停止和重启

```sh
pa status
pa stop
pa restart
```

退出 CLI 不停止后台。配置页“保存并生效”优先在线更新；结构性变更才提示显式重启。`pa restart` 重启整个后台，配置网页会暂时断开。未知执行不会因重启自动重做，失败的在线更新候选也不会随重启自动重试。

日志位于实例的 `state/configuration/manager.log` 和 `business.log`。`pa service logs` 可查询服务日志；Windows SCM 启动故障还需查看 Windows 事件查看器。

## 安装系统服务

需要开机启动时安装。先结束正在运行的 `pa run` 前台进程，或执行 `pa stop` 停止 CLI 自动启动的后台，再登记服务。

### Windows SCM

以实例拥有者账号打开管理员 PowerShell，进入仓库根目录，重新设置安装环境 PATH 并选择实例。账号需要实际密码及“作为服务登录”权限，不能使用 LocalSystem 或仅凭 Windows Hello PIN。

```powershell
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
pa use "$PWD\instance"
$ServiceAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
pa service install --account $ServiceAccount
pa service start
pa service status
```

若已有实例位于其他目录，`pa use` 指向原实例路径。安装时会询问密码并校验权限；登记为开机自动启动，需单独执行 `start`。

Windows Server 2025 的核心进程已验证，SCM 安装、注销和整机重启仍待实机验收。部署为服务后需检查这些生命周期行为；详见[平台记录](../archive/platform-acceptance-2026-10.md)。

### Linux systemd 用户服务

以实例拥有者运行，不使用 `sudo` 执行应用命令：

```sh
pa service install
pa service start
pa service status
```

如需开机启动且注销后继续运行，由管理员为该账号启用 linger：

```sh
sudo loginctl enable-linger "$(id -un)"
```

### 服务操作

`pa service` 支持 `status`、`logs`、`start`、`stop`、`restart`、`uninstall`。卸载前先停服；卸载只移除服务登记，不删除实例或应用。服务引用固定的 Python、Node 和实例路径，移动它们后需重新登记。

## 备份和升级

先查看版本及插件兼容性，再检查准备安装的发行包：

```text
pa version
pa plugins
pa upgrade-check <personal_agent版本包.whl>
pa upgrade-check <personal_agent版本包.whl> --plugin-wheel <独立插件版本包.whl>
```

`pa version` 无需实例；其他命令作用于选定实例。`pa plugins` 在插件不兼容时仍能显示版本与原因。预检不安装、不重启；出现阻断项应先取得兼容插件或显式取消启用。未启用的旧插件可以保留，版本较旧但兼容的插件不强制更新。配置页“功能与插件”同步显示内核版本、扩展版本与兼容范围；“配置修订”表示设置快照，不是软件版本。

1. 核对在途工作和未知操作，完成收尾后停服。
2. 记录 `git rev-parse HEAD`，备份完整实例，包括配置、`.env`、OAuth 凭据、数据库、用量和工作文件，保持备份私有。
3. 获取目标发布版本，在应用目录重做[安装指南](deployment.md)中的依赖安装；standard 需构建前端并再次安装包。Pi 锁定版本变化时重新执行 `pa install-pi`。保留实例，不执行 `init`。
4. 按版本说明审查配置候选。旧实例可用下面的命令生成候选，命令不修改原配置或运行实例：

   ```text
   pa --config <旧实例>/config/config.toml prepare-config <新候选目录>
   ```

5. 显式应用审查后的配置，启动并检查状态、对话和实际启用的能力。安装路径变化时重新安装服务登记。

候选生成用于补齐旧配置的显式依赖，不保证 Windows 平台兼容；需移除未认证扩展并修正依赖。已有实例的插件列表不会自动更新。

会话迁移前自动生成 `conversation-before-migration-<ID>.sqlite3`，事务失败回滚；归属不明确时停止并要求修复。不要清库或重置 Pi。需要回退时同时使用匹配的旧代码、环境和停服备份，并核对未知外部操作。

发布包和供应商切换的完整流程见[发布与切换](../development/release.md)。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 `pa` 或缺少新子命令 | 检查 PATH 是否指向本次 `.venv`；拉取源码后还需安装包 |
| `Plugin ... does not support windows` | 在独立配置页取消未认证插件并修正依赖；Windows 使用 slim |
| 业务配置损坏，聊天无法启动 | 执行 `pa configuration serve --management <实例目录>/management.toml` 打开独立管理页修复；已有管理进程时直接访问原页面 |
| `requires core`、`requires ... installed ...` | 当前内核或依赖版本不在插件声明范围内，更新插件或取消启用；不要修改版本号绕过检查 |
| `missing or invalid release declaration` | 插件需由作者提供当前格式的 `plugin.json` 并重新打包；仅有旧平台声明不足以启用 |
| `downgrade requires ... backup` | 恢复匹配的旧环境和升级前完整实例备份；不能只装回旧代码或删除版本记录 |
| 模型未配置／调用失败 | 检查账号授权、供应商开关、模型绑定和网页待部署步骤 |
| 端口被占用／后台启动失败 | 检查日志和原有进程；明确修改监听地址，不另起重复实例 |
| 配置保存后未生效 | 查看“生效状态与维护”：等待安全边界、修复后重试，或按提示显式重启／完成外部部署 |
| 服务状态正常但不能聊天 | 同时检查 `pa status`、业务日志和模型实际调用 |

配置修复边界见[配置模块](../../src/assistant_runtime/core/configuration/README.md)；命令用法见 [CLI 指南](cli.md)。
