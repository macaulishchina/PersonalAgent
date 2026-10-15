# Installed third-party MCPs

`servers.json` is the integrations plugin's active registry, tracked in Git.
Use the integrations MCP or `personal-agent mcp` to change it with revision checks.
It contains non-secret startup definitions and policy, not installed packages,
OAuth credentials, verification receipts or device recordings.

| Registry name | Installation | Availability |
| --- | --- | --- |
| `codex_resets` | Remote Streamable HTTP service at `https://codex-resets.com/mcp` | Foreground and background |
| `mobile_mcp` | `@mobilenext/mobile-mcp@1.0.11`, installed under `~/.local/share/mobile-mcp/1.0.11` | Foreground and background |

Mobile MCP uses the installed Node v25.9.0 executable and local Android
platform-tools. Its `PATH`, `ANDROID_HOME` and telemetry switch are non-secret
runtime settings. On another machine, install the pinned package and required
Node/Android tools, then update executable, argument and environment paths using
the manager; committing a definition does not install those dependencies.

These entries were recorded from the existing user-level Codex registrations.
The assistant instance can use them independently with
`external_mcp.inherit_user_servers=false`; other Codex sessions retain their own
configuration. Native runtime tool names are `external_codex_resets` and
`external_mobile_mcp`. Approval defaults to `prompt`; change tool policy explicitly
when authorizing unattended actions. Initialization/discovery is separate from
actually operating a device.
