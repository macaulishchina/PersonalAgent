# Independent plugin example

Build and explicitly install this package into the application's environment.
Enable `sample` in the instance's `plugins` list. The framework discovers the
`personal_agent.plugins` entry point; no source registry edit is required.

Before adapting this example, read the [plugin contract](../../docs/development/plugins.md#版本与兼容声明), [release rules](../../docs/development/release.md#版本规则) and [compatibility checks](../../docs/development/testing.md#版本与兼容性检查).

The shipped example is certified for Linux only. `plugin.json` is included in
the wheel and read by its `PluginDefinition`; static and runtime certifications
must match. The same descriptor declares version `1.1.2`, API 2 and core range `>=0.5.1,<0.6.0`; `plugin_manifest(__file__)` creates the runtime manifest. The plugin release must match the distribution version. Windows and Windows Server refuse to enable this example. Certify a
new platform through an explicit release change after appropriate validation.

For a new release, update `pyproject.toml` and `plugin.json` together. Keep the
entry point ID, descriptor ID and configuration namespace consistent. Retain
`plugin.json` in package data and derive the runtime manifest from it; do not
recreate a manifest with default version values in the implementation. Build
and install the wheel in an isolated validation environment, then inspect
`pa --config <test-config> plugins`. Preflight a framework plus plugin update
with `pa --config <test-config> upgrade-check <framework-wheel> --plugin-wheel <sample-wheel>`.
An enabled sample remains blocked on Windows regardless of version compatibility;
leave it disabled when inspecting its metadata there. Metadata preflight alone
does not establish execution or data migration success.

`configuration.json` separately declares stored data, its independent configuration
page, and when updates take effect. The host renders the page without constructing
the plugin. `prepare_configuration` switches settings at the next request boundary
and restores the previous settings if the shared transaction fails. Existing notes
and accepted reports remain intact. See the application's plugin development guide
for controlled widgets and management actions; plugin JavaScript is not loaded.

This example contributes a packaged skill, read/write tools, namespaced durable
state and an event. It imports only the public plugin API. The note tools make no paid or device requests. The optional report workflow uses the
configured execution model and its shared usage gate: `sample_report` registers bounded
text and submits a read/write-scoped run; `sample_report_status` returns result and report
artifact metadata. It never waits synchronously inside a parent model tool, recursively
executes another agent loop, or bypasses model configuration. Duplicate request UUIDs
reuse the registered input and execution. Enable execution/workfiles (already in both
presets). Real-model calls require the instance's normal authorization and budget. Only trusted packages should be installed; this is not a sandbox.

The tool schemas include valid `examples`; development checks validate these without running
the handlers. Runtime registration warns about and omits invalid documentation examples
while preserving an otherwise callable tool. Dynamic length and request-ID conflicts raise public `ToolFailure` with
the invalid field and confirmed `not_dispatched` status. Unexpected failures retain
uncertainty; the sample never catches them to retry a report. Rewriting the identical
note does not publish another event. Report submission returns an execution receipt;
completion, result and artifacts are obtained through `sample_report_status`.

## 完整结果与按需读取

`sample_report_status` 保留公共执行接口返回的 `result_ref`，不要只取预览字符串，
也不要再次调用 `sample_report` 来补取报告。完整正文可通过同一公共文件接口分页读取：

```python
from assistant_runtime.api import FileRequest

page = await context.invoke(FileRequest("result_read", {
    "id": result_ref["id"], "version": result_ref["version"],
    "path": "/text", "offset": 0, "length": 6000,
}))
# 后续读取使用 page["next_offset"]，直到 page["eof"]。
```

调用身份由宿主绑定，复制引用不会取得读取权限。过期、无权限和完整性错误应直接说明，
不重新执行原任务。工具返回无需自行截断或生成临时文件：框架为大结果保留完整内容，
模型使用 `read_result` 按需读取。可选的展示摘要契约见
[插件开发规范](../../docs/development/plugins.md)，缺少摘要时仍能正常使用确定性节选。
