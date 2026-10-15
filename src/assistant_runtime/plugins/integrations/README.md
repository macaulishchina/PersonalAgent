# Third-party MCP integrations

The optional `integrations` plugin owns this assistant instance's third-party MCP
registry and management capability. Codex owns native MCP transport, authentication
and tool dispatch. There is no second tool proxy, package installer, model session,
device inventory or global Codex configuration writer.

Install the `integrations` extra in the same `.venv` and add `integrations` to an
explicit `plugins` list. It is included in the default voice plugin set.

```toml
[external_mcp]
max_servers = 32
inherit_user_servers = false
# registry_path = "/absolute/private/path/servers.json"
```

The default registry is private `state/mcp/servers.json`. Management writes an
atomic, mode-0600 JSON file with a global optimistic revision; configuration never
belongs in Git. A failed write leaves the last good file and in-memory state intact.
Manual edits require increasing the registry revision, followed by `reload`.
Server removal drops private values but retains a disabled identity, preventing an
old resumed context from resurrecting that registration. Removal does not uninstall
packages or delete user data.

`inherit_user_servers=false` disables user/project MCP definitions found in each
context's effective Codex configuration. The optional true setting explicitly keeps
existing user/project MCPs under their existing policy in foreground/background;
they are reported as inherited, not managed registrations. Verification and memory
maintenance disable inherited servers regardless. Managed servers always retain
this registry's explicit scopes and policies. Global Codex settings and plugins
remain owned by Codex; this plugin is not an operating-system security sandbox.

## Definition and use

Use the `integrations` MCP or `personal-agent mcp` CLI. Both dispatch the typed
`IntegrationRequest` to the same plugin. The manager exposes list, inspect,
configure, enable/disable, remove, verify, apply, reload and OAuth login tools.
Read the current global `revision` before editing. Identical configure retries are
idempotent; stale changes fail rather than overwrite someone else's configuration.

```sh
personal-agent mcp list
personal-agent mcp configure '{"name":"example","revision":0,"changes":{"transport":{"type":"stdio","command":"/absolute/path/to/server","args":[]},"contexts":["foreground","background"],"allow_tools":["search"],"approval":"approve"}}'
personal-agent mcp verify '{"name":"example","context":"foreground"}'
personal-agent mcp verify '{"name":"example","context":"background"}'
personal-agent mcp apply
```

`transport` is one of:

- `stdio`: executable `command`, optional `args`, absolute `cwd`, private `env`
  values and `env_vars` references to the host service environment. The command is
  executed directly, not interpreted as a shell string. Repeated/empty arguments
  are preserved. Default cwd is persistent `state/mcp/servers/<name>`; background
  task cwd changes never relocate the server's files.
- `http`: Streamable HTTP `url`, optional private `headers`, `env_headers`
  (header name to host environment variable) and `bearer_token_env`. URL credentials
  and fragments are rejected. Native Codex OAuth is available through
  `begin_mcp_login`; the user follows its authorization URL and verifies again.
  Tokens remain in the native provider's credential store, never tool responses.

Top-level definition fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Enable future contexts |
| `contexts` | foreground, background | Explicit usage scopes |
| `allow_tools` | `null` | All tools; an empty list permits none |
| `deny_tools` | `[]` | Excluded tools; denial wins |
| `approval` | `prompt` | prompt / approve / reject |
| `tool_approvals` | `{}` | Per-tool prompt / approve / reject |
| `startup_timeout_seconds` | 30 | Connection startup bound, 1–120 |
| `tool_timeout_seconds` | 60 | Native tool call bound, 1–120 |

`reject` denies calls (it is translated to native disabled-tool policy); it does
not invent a provider approval enum. A server-wide reject denies every tool.
Third-party tools never inherit the preapproval of internal device/memory tools.
With a noninteractive approval policy, prompt-required calls may be declined;
authorize only the intended tools explicitly when unattended use is wanted.

Top-level configure changes merge with the existing definition; a supplied
`transport` replaces that entire object. Credentials should use environment
references; do not put secrets in command arguments. Inspection returns argument
counts, environment/header names and HTTP origin, without argument values,
environment/header values or URL paths/query strings.

## Assistant-directed installation

The initiating assistant can read provider documentation, install pinned packages
through its existing authorized execution tools, configure this registry, verify
both intended scopes, request application, and then call an appropriate native MCP
tool within the user's authorization. Configuring a command does not install it.
Use the server's persistent work directory for dependencies; use the task's own
scratch for temporary downloads/scripts. Do not modify shared Codex configuration.

A background installation task declares `resources=["mcp_management"]`. This reuses
the task scheduler's resource lock and child-scope rules; only its active execution
receives management tools and the capability checks that resource again at call
time. Ordinary background jobs can inspect/verify their background MCP scope, and
use native tools enabled for that scope. They cannot change shared registrations.
Ended tasks lose management permission. These are trusted-host authorization
boundaries, not a filesystem sandbox against arbitrary installed code.

## Application and validation

Saved, verified and applied are separate states. A background run captures the
current tool configuration and records `data.mcp_revision` when dispatched; later
changes apply to new runs or explicit continuations, never to an executing turn.
Main refresh waits for idle model and voice. It unsubscribes and resumes the same
persisted foreground thread; the installed protocol ignores new MCP overrides on
an already loaded thread. No input/Goal is replayed, microphone/output is switched,
or service restarted. Voice start and refresh share the foreground dispatch lock;
voice remains marked active until transport teardown completes.

Refresh failure restores the last confirmed configuration when possible. Automatic
retries for that bad revision stop; the main conversation can use the previous
configuration. Correcting it or explicit `apply` retries configuration, not user
work. An unconfirmed rollback is surfaced instead of claiming availability.
If a management caller disconnects during refresh, the bounded refresh/rollback
transaction finishes before releasing the foreground lock.

`verify_mcp_server` starts a real, isolated native provider connection, discovers
bounded tool/schema/auth metadata, and always cleans up its ephemeral context.
It calls no application tools and starts no inference turn. It can explicitly test
a disabled registration before enabling it. Excluded scopes fail. Verification
receipts bind to the server revision and configuration content, and become stale
if configuration changes during the probe. At most four verifications can be
pending, with two running concurrently; cancellation and shutdown retain cleanup
ownership. Native OAuth waits are separately bounded and owned.
After applying, use the exposed `external_<name>` tools to test the intended action;
discovery alone does not prove a phone/camera/device action succeeded.

Third-party device tools are additional capabilities. They do not replace fixed
voice bindings, enter captured data as user instructions, or automatically join the
framework's device resource leases. Integrate an adapter when those device-owner
contracts are needed; do not wrap every external MCP as a duplicate device plugin.

## Acceptance

```sh
.venv/bin/python scripts/check.py
.venv/bin/python scripts/verify_integrations.py
```

The isolated acceptance uses synthetic stdio and localhost Streamable HTTP MCPs
and the installed Codex App Server. It discovers both configured scopes, dispatches
real synthetic tools without model inference, rejects excluded scope, refreshes
the same foreground thread, reports a missing executable, disables maintenance
tools and cleans up processes/threads. It touches no production registry, physical
device, user conversation or model API. Unit tests cover optimistic concurrency,
redaction, atomic-write failure, stale probes, bounded cancellation, scoped task
management, OAuth lifecycle and configuration rollback. Real vendor pairing,
OAuth authorization and device actions require that integration's own acceptance.
