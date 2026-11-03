# Work files and registered inputs

This core plugin owns working directories, artifact listings, scratch retention and
registered input snapshots. It does not depend on conversation, devices or task scheduling.
Both presets expose the same `FileRequest` capability and `files put|get` CLI.

`put` accepts `name`, base64 `data`, and optional `kind` (`file` or `image`). Each input
is nonempty and at most 2 MiB. The host binds ownership; there is no arbitrary path,
URL, credential or owner parameter. Registration copies the bytes into private instance
storage and records SHA-256, size and a stable reference. `get` returns metadata only.
Plugins must declare `uses=(FileRequest,)` and use their bound context.

Executions accept up to eight references, including at most four images. Submission
checks ownership; dispatch verifies the captured digest and copies the inputs to that
execution's scratch area. Names are display metadata, not destination paths. Changed,
missing or cross-caller inputs cannot silently become different model input. Inputs are
not automatically replayed after restart. Original registered snapshots remain available
until instance storage is explicitly maintained; scratch retention never deletes them.

Generic files require no optional decoder and are available to allowed file tools. Image
registration requires Pillow (included in standard, optional for slim), validates PNG,
JPEG or WebP and bounds pixels to 20 million. Images also require an image-capable model;
they are sent as native image input, never silently converted to text. No audio dependency
is introduced. This is application resource ownership, not filesystem sandboxing.

The existing web/task media registry is now owned here as well. Its files and IDs remain
unchanged. Public execution accepts these image IDs through the same capture/digest
contract; trusted task ownership can grant the producing work access to registered
references. Model arguments cannot create these grants or choose arbitrary filesystem paths.
The 2 MiB upload limit applies to the public base64 registration endpoint; existing Web
image normalization/10 MiB admission remains unchanged.

## Immutable execution publications (development candidate)

`FileRequest` / `pa files` additionally provide:

| Action | Arguments |
| --- | --- |
| `finalize` | `finalize_id` (stable UUID), `execution_id`, `name` (one artifact filename), `expires_at` (UTC Unix seconds) |
| `artifact_get` | `id` |
| `artifact_read` | `id`, optional `offset`, `length` (1..1,048,576 bytes) |
| `artifact_retain` | `id`, later finite `expires_at` |
| `artifact_release` | `id` |

The source is an owned execution's registered artifact directory, never an arbitrary path.
A known terminal outcome is required. Unknown/active executions cannot publish through
this initial interface. The caller uses existing execution ownership; this is not the
future multi-party resource-grant authorization API.

Publication returns an independent stable ID, execution ID, bytes, SHA-256, state and expiry.
A duplicate finalize intent returns the original publication; changed fields conflict.
Changing the source or publishing its name again with another ID does not mutate old bytes.
Reads return base64 data, offset/next_offset and eof, with the fixed total size and digest.
Disconnected archives can resume and verify the final digest; download is not an archival
acknowledgement. Publication expiry is explicit and independent of scratch retention.

The initial snapshot backend is **Windows only**, using a pinned directory and a read handle
that denies concurrent writes/deletion/replacement. Hard links and reparse sources are
rejected. Linux's existing input/work-area functions are unchanged; frozen publication is
explicitly unavailable there until a suitable backend is implemented and accepted.

Each object is bounded to 64 MiB, each read to 1 MiB. `work_files.artifact_storage_bytes`
sets the total publication allowance (default 512 MiB, 0 disables new publications).
Admission conservatively reserves 64 MiB for a publication in progress. Lowering the limit
only affects new admissions; it does not remove existing objects. Release/expiry collection
is serialized with reads and finalization; tombstones preserve idempotency after collection.

Bytes are flushed before the published receipt is committed. Incomplete finalizations found
on restart become `unavailable`; retrying the same intent cannot silently snapshot a changed
source. Read integrity is checked before returning bytes. Host storage corruption is reported,
not served as changed content. This publication API does not sandbox existing full-permission
executions and does not certify arbitrary host/plugin writes as impossible.

Source and CLI integration are tested on Windows Server with synthetic artifacts. Cross-round
resource leases, grant revocation, reliable external archival acknowledgement and the full
context lifecycle remain part of the joint design; this is the publication foundation.

## 工具图片：统一保留与交付回执

Pi 工具返回的 PNG/JPEG/WebP 进入本模块的原有媒体登记，不调用另一个
截图／保存工具，不抓取 URL，也不采用工具传入的任意目标路径。原图按接收字节
保存（不套用聊天上传的 JPEG 缩放），同时进入所属执行的 `artifacts`；模型可使用
框架回执中的附件引用、文件名、大小和 SHA-256。原有聊天上传仍按原规则处理。

登记身份由执行服务绑定，包含执行、当前轮和工具调用 ID；同一调用的相同结果
复用附件，变更结果拒绝覆盖。回执自身不保存 Base64。附件授权沿用原有输入资源
所有权和 grant 机制。此处是已有工作产物的留存，不是 `finalize` 不可变发布，
也不扩大外部 MCP 的文件权限。受控执行目前不走此自动保存入口，避免绕过资源 broker；
保留原始工具图片并返回明确的附件登记失败。

单次登记至多四张，每张沿用 10 MiB／2000 万像素约束，需安装 Pillow。
不支持的格式、解码依赖缺失或磁盘故障返回独立的 `failed`／`partial` 媒体回执，
不能把原操作改判为失败或自动重做。取消后不再向已结束执行投递附件；已收到的
文件证据可以保留。现有工作目录及媒体维护规则继续适用。


## 统一结果存储与按需读取

收到的工具正文、结构化内容、自动留存图片和最终报告使用稳定 `id/version`。
`complete_received` 只描述框架收到的内容，`upstream_complete=null` 表示不能判断上游
是否截断。业务状态继续归执行／业务插件；留存或投递失败不更改业务成功，不触发重放。
短内容内联 SQLite，大内容先在私有 `work/results` 写入完整字节并同步，再提交索引。
同一身份不同内容拒绝覆盖；重复交付不重造已经过期的内容。
普通结果存储在 Linux、Windows 共用，不依赖上面的 Windows `finalize`。

公共 `FileRequest`（及 `pa files`）提供：

| 操作 | 参数与行为 |
| --- | --- |
| `result_get` | `id`；元数据、完整性、保留状态；不延长期限 |
| `result_read` | `id`，可选 `version`、`format=text/json/bytes`、`path`、`offset`、`length`；返回位置及 `eof` |
| `result_usage` | 操作者查询；可选 `ids` 预览指定结果删除；不延长期限 |
| `result_collect` | 操作者清理；`apply=true` 必须带预览返回的 `version`，可选 `ids` |

文本默认读取 `/text`，`path=""` 可分段查看完整 JSON；JSON 路径为 RFC6901，数组
按元素分页、对象按键值对分页。禁止查询代码。默认正文 6000 字符，程序单块正文
最多 1 MiB；大元素需按子路径或文本读取。读取后更新访问时间，目录列表不更新。
执行身份由宿主绑定；引用或路径不授予权限。已授权任务查询可授予该报告读取权。

配置后台“文件与记忆”提供结果保留选择、容量、清理预览和明确确认：

```toml
[work_files]
result_retention_days = 0      # 默认永久；仅支持 0、7、30、365
result_storage_bytes = 536870912 # 默认 512 MiB；0 禁止新留存
result_max_bytes = 67108864    # 单份默认 64 MiB
```

只有已应用的配置参与维护，保存不会立即删除。保留期按创建与最后成功读取较晚者
计算。容量降低不立即删除旧结果；永久保留不会自动腾挪；自动清理仅删除到期且
无保护的内容，容量不足单独报告。短结果内联也计入额度。

执行、任务、通知模块分别拥有保护关系，工作文件模块只调用所属模块的核对接口。
活动、等待澄清、未知执行及未投递报告受保护；缺少所属模块时保守保留。已结束
且已确认投递后可解除保护。内容清理保留最小墓碑，读到过期引用不会自动补执行。
共享图片所有结果引用均可清理后才删除。只清理自动登记的 `tool-image-*` 原图副本；
用户上传、外部源文件、其他独立产物和已修改的产物不受此策略影响。历史静态聊天
引用不永久阻止到期。不会扫描旧目录补造结果。

任务通知冻结同一结果版本；投递重试只投递这份报告。网页默认折叠，按块读取完整
正文或 JSON；任务报告可跳转对应任务。执行轨迹仍为有界过程记录，不等于结果仓库。

### 验收记录（2026-11-03）

- 全量检查：1426 项测试通过检查，33 项受平台条件限制跳过；随后收尾改动的
  定向回归 96 项通过，涵盖结果、任务及对话。未将 Linux 测试当作 Windows 实测。
- `tests/test_result_delivery.py` 与 `tests/test_tool_media.py` 使用真实 Pi、合成
  Responses 服务和合成 MCP，覆盖主对话、后台任务和通用执行入口；大报告、尾部
  错误及原图可读取，分页不重新调用原工具。未调用真实供应商、摄像头或媒体写接口。
- 隔离浏览器验证：保留期限保存及重载、清理预览与确认、默认折叠不请求、分页追加、
  读取失败后单独重试，以及完整 JSON 切换通过，无浏览器脚本错误。
- wheel 声明和资源检查通过；从源码目录之外加载 wheel 内容，结果存储、分页读取及
  管理／聊天前端资源可用。生产配置、数据和服务未变更。

相关回归可运行 `scripts/check.py --test test_result_delivery --test test_tool_media
--test test_conversation --test test_tasks`。正式部署按既有发布流程独立进行。
