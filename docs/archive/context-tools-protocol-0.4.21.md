# 0.4.21 空助手文本重放修复与验收

日期：2026-11-05。源码基线 `8086391`；固定 Pi 1.4.0。候选包通过定向及安装验收，未部署。

## 已确认的问题与归因限制

KFC 报告的执行 `553a3aaf79384af184dd26f68ddca59e` 在两个 MCP 调用成功返回后发生 HTTP 400。公开 trace 的 seq45–48 与用量中的两次请求支持这一事实，不能据此归因为业务工具没有返回。

对该隔离会话只读检查发现：工具 ID 在原生历史中正确配对，但同一助手消息的内容排列为 `thinking → toolCall → toolCall → text`，最后的 `text` 长度为 **0**，带有独立文本项目标识。用锁定 SDK 重建下一次出站表示，会在 `function_call` 与 `function_call_output` 之间生成额外空助手消息。

严格本地网关能稳定拒绝这一结构；旧提交 helper 的私有副本在新回归中失败，修复后通过。原历史重建对照中，空助手消息从 1 个变为 0 个，调用／返回元数据、非空文字和源文件摘要不变，SDK 补造的工具返回数量为 0。

**这是已修复的可确定协议异常，不等于原真实 HTTP 400 的唯一根因已完全证明。** 未取得原故障第二次请求的原始出站包；此次真实新请求（包括未修复副本的若干对照）没有再次触发原网关 400。用户确认网关与上游未变更，不能据此宣称网关更新了。历史检查、重建、严格合成复现与真实供应商回归的证据分开记录。

## 修复及影响范围

`core/pi/accounts.mjs` 在既有 `structuredClone` 后，仅为自定义 `openai_responses` 连接过滤助手内容中的零长度文本块。

| 范围 | 结论 |
| --- | --- |
| 普通对话、独立执行、后台任务 | 共用此发送适配，均获得修复；触发条件不是必须存在快照 |
| 同模型／跨模型快照、已有历史恢复 | 发送副本不再重放空助手项目；持久历史、快照内容不改写 |
| 模型与工具 | 不按 Kimi 名称特判；任何该协议连接产生此空项目都可能涉及，非所有 MCP 调用必现 |
| 非空文字、空格、推理状态、调用 ID、返回结果 | 不删除、不改写、不调整原顺序，不补造返回 |
| 原生账号、Chat Completions、Anthropic、Gemini | 本次分支不改变它们的序列化 |
| 公共 API、插件、存储 | API 2／数据格式 1 不变，无迁移；插件主版本与兼容范围不调整 |
| 已失败／未知执行 | 保持原状态，不自动重试、重派或重新执行工具 |

当前修复不清理数据、不替换源模型、不使用空背景规避错误。恢复原失败任务应另行核对其操作回执，并使用已有、明确授权的继续／重试流程。

## 验收

110 项定向回归通过：`test_context_snapshot` 18 项、`test_pi_skeleton` 34 项、`test_pi_waiting` 5 项、`test_model_accounts` 17 项、`test_models_transport` 8 项、`test_model_routes_native` 2 项、`test_tool_media` 6 项、`test_versions` 20 项。新增压缩夹具首次因历史太短失败，补足独立轮次后定向复跑通过；不是隐藏跳过。静态格式、lint、类型与发布声明检查通过。本次不是全量发布门禁。

新增回归验证：带工具历史后连续两轮双调用、空文本／非空文本、失败工具结果、普通 MCP 会话、同／跨模型快照、严格模型路由、重启继续、真实 Pi 压缩后导入，以及公开 `snapshot → TaskRequest.create(spec.context)` 路径。原历史中的空块及真实文字仍保留，已完成工具不重复执行。

```powershell
.venv\Scripts\python.exe scripts/check.py --test test_context_snapshot --test test_pi_skeleton --test test_pi_waiting --test test_model_accounts --test test_models_transport --test test_model_routes_native --test test_tool_media --test test_versions --jobs 3
```

候选 wheel 在全新目录构建、安装，并用同一 `.venv` 从安装目录运行 3 项真实 Pi 关键检查，全部通过。核对包内 `accounts.mjs` 与源码逐字节一致，网页资源只含当前源码资产。无依赖变更，因此不另建虚拟环境。

## 真实模型证据

新隔离实例、实际已配置连接、源历史由本地合成供应商建立，目标模型真实调用。只调用合成 MCP `catalog/history`，不触碰真实业务工具，不重放原执行。该验收是 Pi 适配链路验证，未冒充完整 KFC 业务联调。

| 目标模型 | 实际目标请求 | 结果 |
| --- | --- | --- |
| `kimi-k3` | 2，均 HTTP 200 | completed；catalog/history 各执行一次、按 ID 收到成功返回 |
| `gpt-6.1-sol` | 3，均 HTTP 200 | completed；catalog/history 各执行一次、按 ID 收到成功返回 |

最终两例每项最多 3 次请求、每次输出 512 token，禁用重试；unknown_requests 均为 0。诊断与验收累计观察到 26 次真实上游请求，全部为新测试请求；本地合成响应已排除。另有诊断运行按请求额度停止，单独保留为额度停止，未算作成功验收。一次对照对齐原执行的无限制输出设置，采用最多 2 请求、30 秒／请求、16 KiB 响应接收保护，完成后未继续扩大。配置缺少价格，金额未知；零估算不表示免费。

## 本地证据与候选包

证据根：`state/acceptance/context-tool-protocol/`（忽略目录，不随源码提交）。

- `red-baseline/result.json`：旧 helper 的精确空文本回归失败。
- `original-projection-comparison.json`：原会话只读重建前后对照，**不是原始抓包**。
- `real-final-validation.json`：最终两模型真实调用／返回核对。
- `request-accounting.json`：全部诊断的真实请求计数及额度停止。
- `package-final/acceptance.json`、`installed-check.log`：3 项安装验收。
- `package-final/dist/personal_agent-0.4.21-py3-none-any.whl`。

候选包 SHA-256：`d948ba75e937000ff653963475c22098cde0fc46005de71c5723678a17e2456c`。

KFC 后续在隔离实例使用候选包复验原完整业务路径；若原 400 再现，保留请求数、阶段、上游请求标识与安全协议结构，继续定位，不能宣称此次已证明所有同类错误都已消失。当前未部署、未重启任何现有服务。
