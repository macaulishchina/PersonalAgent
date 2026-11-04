# 等待澄清任务取消修复候选（0.4.16）

日期：2026-11-04。针对 KFC 在 PA 0.4.15 隔离验收中发现的 `waiting_user` 任务停止超时。未部署、未重启或替换在用服务，未改动 KFC 源码及原失败证据。

## 原因与修复

真实 Pi 子进程与本地合成模型已复现旧实现：`run.cancel` 的 `abort` 命令在 10 秒处超时。
`request_user_input` 忽略工具取消信号，`ctx.ui.input` 持续等待；Pi 的 abort 则等待执行结束。
将信号传入输入等待后，工具可以退出，但原普通异常又被保守识别为未知外部操作，因此一并改为由输入工具返回确定的 `user_input_cancelled` 所有者回执。

执行结束时清理对应 worker 的原生问题关联，并清理执行服务、Pi jobs 与任务快照的问题记录。
任务停止确认期间会过滤普通状态回调，因此确认停止路径也必须显式清空问题，不能只依赖终态回调。
不会清理其他 worker 的问题，也不会将真正未知的外部操作标成取消成功。

## 兼容性

- 原 `TaskRequest("cancel_run", {"run_id": ...})`、`pause_run`、`supplement` 不变。
- 任务取消终态仍为 `canceled`，公共单次执行仍为 `cancelled`，Pi 原生回合为 `interrupted`。
- 已结束问题的迟到回答被拒绝，不投递给新的问题。
- 保留未知结果隔离、共享用量保护及禁止自动重放的语义；不增加超时、不新增专用接口。
- 公共 API 2、data_format 1、配置及扩展版本保持；不需要插件大版本升级，不回写修复旧 unknown 记录。

## 验证与复验

新增 `tests/test_pi_waiting.py` 使用真实 Pi 和本地合成模型，覆盖取消、暂停、重复取消、回答竞争、问题与容量释放、另一任务及前台会话继续、重启后终态与调用次数保持。
定向回归包含任务、Pi、执行服务/准入/回执、会话和模型错误保护：244 项中 243 通过，1 项因 Windows 符号链接权限跳过。版本与插件边界另有 55 项通过，共 299 项、298 通过、1 跳过；不将这些检查称为全量发布门禁。插件边界源码扫描初次因 Windows 默认 GBK 解码既有 UTF-8 源码失败，设置 `PYTHONUTF8=1` 后隔离重跑该模块 14 项通过，未修改业务或放宽断言。

显式真实模型复验入口（使用已有授权 API 配置，产生少量模型调用；输出目录必须不存在）：

```powershell
.\.venv\Scripts\python.exe -B scripts/verify_waiting_cancellation.py `
  --config instance/config/config.toml `
  --models gpt-6.1-sol gpt-6-luna `
  --output state/acceptance/cancel-waiting/new-real-run --confirm-paid
```

本次真实复验使用 `gpt-6.1-sol`、`gpt-6-luna` 和前台 `deepseek-v4.1-flash`：两个任务先等待，前台正常完成，给 luna 补充后完成，再停止 sol；停止确认耗时 **0.032 秒**，返回 canceled。重复停止、问题/容量释放和干净重启后的查询均通过。
证据保存在忽略目录 `state/acceptance/cancel-waiting/source-real-verified/summary.json`，原报告及 `E:/runtime/multi-ai-lite-20261104/` 证据保持原样。

最终修复 wheel：`state/acceptance/cancel-waiting/package/final/dist/personal_agent-0.4.16-py3-none-any.whl`。
SHA256：`92f3114ef12765d283f3ce26ba140acc027b397ad00303393a38f7c525e47315`。
实际 wheel 的 14 个核心与 14 个扩展声明检查通过；离线安装到全新私有目标，从安装目录加载 Pi 后再次运行上述 5 项回归全部通过，所有修复源码与包内字节一致。证据为 `package/final/installed-summary.json`。私有打包首个候选遗漏 web 描述文件，已拒绝使用，最终候选修正过滤并通过检查；仅使用上述 final 包。
前端输入未修改，沿用 0.4.15 已验收包的前端资源，不重建在用静态产物。此包用于 KFC 后续独立复验，不代表已经完成生产发布或部署。
