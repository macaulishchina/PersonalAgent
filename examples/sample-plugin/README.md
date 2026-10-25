# Independent plugin example

Build and explicitly install this package into the application's environment.
Enable `sample` in the instance's `plugins` list. The framework discovers the
`personal_agent.plugins` entry point; no source registry edit is required.

Before adapting this example, read the [plugin contract](../../docs/development/plugins.md#版本与兼容声明), [release rules](../../docs/development/release.md#版本规则) and [compatibility checks](../../docs/development/testing.md#版本与兼容性检查).

The shipped example is certified for Linux only. `plugin.json` is included in
the wheel and read by its `PluginDefinition`; static and runtime certifications
must match. The same descriptor declares version `1.0.0`, API 1 and core range `>=0.3.0,<0.4.0`; `plugin_manifest(__file__)` creates the runtime manifest. The plugin release must match the distribution version. Windows and Windows Server refuse to enable this example. Certify a
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
