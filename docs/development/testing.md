# 开发与验证

先阅读 [AGENTS.md](../../AGENTS.md) 和[架构](architecture.md)，在当前项目 checkout 和同一个 `.venv` 中开发。测试使用独立临时实例，不修改运行实例。

## 开发环境

部署安装默认使用 `--no-editable`；开发环境可省略它，并启用所需依赖组。确认不在运行中的生产环境同步依赖。

以下命令在仓库根目录运行。`python` 必须来自项目 `.venv`：Windows 可用 `.\.venv\Scripts\python.exe`，Linux 可用 `.venv/bin/python`。

## 自动检查

```sh
python scripts/check.py --list
python scripts/check.py --group execution --group tasks --group models
python scripts/check.py --test test_integrations.AdapterTest
python scripts/check.py --static-only
python scripts/check.py
```

按改动范围先做针对性检查，最终源码／测试／依赖修改后、提交或部署前运行全量检查。已通过的检查在输入未变时继续有效；纯文档修改检查链接、命令和表述，不重复整套运行时回归。

测试按模块进程并发，默认最多 3 个；共享资源或诊断问题时使用 `--jobs 1`。真实设备、供应商和已安装服务验收单独执行。

## 原生平台与安装包

使用已安装好的 Python 和 Pi，在应用目录按需运行：

```sh
python scripts/verify_native_deployment.py
python scripts/verify_native_deployment.py --installed
python scripts/verify_native_chat.py
python scripts/verify_native_chat.py --installed
```

源码与安装包分别记录结果。脚本使用独立临时实例；Linux 检查 slim／standard，Windows 检查 slim 成功和 standard 拒绝。正常退出或捕获异常时清理测试目录；强制终止留下的目录按日志路径核对后处理。

真实 Pi 子进程和本地合成模型可以验证工具、上下文、澄清、取消及恢复，不代表真实供应商、物理设备或系统服务通过。不要为获得绿色结果删除用量限制或隐藏缺失依赖。

## 如何记录结果

分别列出：

- **通过**：实际运行且满足断言或验收条件。
- **跳过**：测试明确跳过，注明平台、权限或能力条件。
- **依赖错误**：缺失可选依赖导致的导入／执行错误，不能计为通过。
- **新增失败**：相对于已知基线新增的失败或错误。
- **未验证**：没有实际执行的供应商、设备、OS 或服务生命周期。

历史数据见[平台验收记录](../archive/platform-acceptance-2026-10.md)。该记录的最终全量检查仍有 12 项既有依赖错误；不因本轮文档整理改写为通过。

私有回执和实验留在忽略的实例存储或临时目录，只在明确要求时生成报告。发布验证见[发布与切换](release.md)。
