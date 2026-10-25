# Independent plugin example

Build and explicitly install this package into the application's environment.
Enable `sample` in the instance's `plugins` list. The framework discovers the
`personal_agent.plugins` entry point; no source registry edit is required.

The shipped example is certified for Linux only. `platform.json` is included in
the wheel and read by its `PluginDefinition`; static and runtime certifications
must match. Windows and Windows Server refuse to enable this example. Certify a
new platform through an explicit release change after appropriate validation.

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
