# 发布与切换

日常升级步骤见[日常维护](../guides/operations.md)。本页用于发布包交付、配置迁移及生产切换；历史私有实例参数不作为发布默认值。

## 1. 准备候选

记录代码提交、依赖锁文件和配置版本。在私有候选目录保留原实例 `root`、凭据引用、注册表和用途绑定，不用空模板覆盖已有配置。

旧配置可用 `pa --config <旧配置> prepare-config <候选目录>` 生成可审查候选。核对 `conversation`、`cli` 及其依赖；Windows 默认使用 slim，只启用声明支持 Windows 的插件；standard 能否启用由所含插件的实际认证决定。模型、任务和记忆只配置实际启用的用途，不隐式启用供应商或回退。

在应用目录使用当前 `.venv` 的 Python 预检：

```sh
python -m scripts.check_pi_release --config /private/candidate/config.toml
python -m scripts.verify_integrations
```

预检检查本地配置、凭据引用、注册表、技能和 Pi 版本，不证明账号有额度、供应商可用或设备已验收。

## 2. 验证实际启用的能力

最终代码、测试和依赖修改后运行[开发检查](testing.md)。真实供应商检查使用授权的账号和费用范围、独立临时实例及明确请求上限：

```sh
python -m scripts.verify_models --config /private/test/config.toml \
  --model ACCEPTED_ALIAS --case tools --max-requests 3 \
  --max-output-tokens 1024 --confirm-paid --output /private/tools.json
```

按需检查 `text`、`image`、`memory`；任务委派使用 `scripts.verify_pi_host` 的显式主／任务模型和请求上限。只记录实际通过的用途，不将模型目录、登录成功或本地合成测试当作供应商验收。

隔离网关测试使用独立实例 root、私有鉴权、回环端口和 Docker 项目。准备工具会写入指定 root 的网关文件，不能用生产 root 生成测试配置。OAuth 轮换令牌不复制到测试实例。

## 3. 构建与安装

按[wheel 交付](../reference/installation.md#wheel-交付)导出锁定依赖并构建。确保构建目录没有遗留旧模块，检查包内的配置模板、平台声明、技能、Pi 资源，以及 standard 所需前端产物；不得包含私有实例文件。

使用安装包运行原生临时验收，记录提交、包摘要、配置版本、通过／跳过／错误及实测边界。源码通过不代表安装包通过；未提交候选也不作为已发布版本。

## 4. 切换运行实例

1. 核对在途工作、未知执行和外部操作回执，明确完成或停止策略。
2. 停止后台并确认所属工具收尾，备份完整实例、原代码／环境版本和服务登记。
3. 安装审查后的包和配置，保持实例 root 与数据；不要并行启动新旧两个业务实例。
4. 网关需要变更时显式更新原网关并检查鉴权健康。Python／Node 或服务入口变化时重新登记服务。
5. 在实际服务账号及环境下检查 `pa models check`、`pa model-accounts status --account NAME`，再启动。
6. 确认配置页、CLI 和实际启用的网页、任务、MCP、记忆等能力；完成工作后再验证一次正常重启。新设备动作、批量下载和周期计划需另行明确。

保留已有任务状态及回执。重启、切换供应商或恢复旧版本都不能用新 ID 重放未知工作。

## 5. 失败与回退

停止新派发，保留错误、用量回执和未知操作状态，再修复配置或失败能力。回退需要匹配的旧代码、环境和停服备份，不只替换 TOML；恢复数据库前先核对外部副作用，不自动重启有已知失控行为的历史执行器。

当前交互式 MCP OAuth 尚未实现；使用已支持的 stdio 或 HTTP 凭据方式并验证连接，不能以临时登录代替实例级凭据生命周期。认证和供应商限制详见[模型](../../src/assistant_runtime/core/models/README.md)与[MCP 模块](../../src/assistant_runtime/core/integrations/README.md)。
