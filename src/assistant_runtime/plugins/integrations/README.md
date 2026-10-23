# Third-party MCP integrations

The optional `integrations` plugin owns this assistant instance's third-party MCP
registry and management capability. Pi owns native MCP transport, authentication
and tool dispatch. There is no second tool proxy, package installer, model session,
device inventory or global Pi configuration writer.

Install the `integrations` extra in the same `.venv` and add `integrations` to an
explicit `plugins` list. It is included in the default plugin set.

```toml
[external_mcp]
max_servers = 32
inherit_user_servers = false
# registry_path = "/absolute/private/path/servers.json"
```

The default registry is private `<instance root>/config/mcp/servers.json`, including
MCPs installed by the assistant. Only the empty reusable template in
`config.example/mcp/servers.json` belongs in Git. Management
writes an atomic, mode-0600 JSON file with a global optimistic revision. A failed
write leaves the last good file and in-memory state intact. Credential values never
belong in this file: use environment references or the native provider credential
store. Literal environment/header values are for non-secret settings only. Server
work directories, verification receipts and raw acceptance artifacts stay private
in ignored `state/`; a registry does not package its dependencies.
Manual edits require increasing the registry revision, followed by `reload`.
Project-local Coding AI files are not discovered. A custom private path can be
selected explicitly with `registry_path`; no legacy registry is imported automatically.
Server removal drops private values but retains a disabled identity, preventing an
old resumed context from resurrecting that registration. Removal does not uninstall
packages or delete user data.

Pi runs with isolated settings and an explicit MCP snapshot; it does not import global user registrations. `inherit_user_servers` controls only executor-reported inherited definitions when that capability exists; Pi reports none. All managed servers use this registry's explicit scopes and policies. This is not an OS sandbox.

## Definition and use

Use the `integrations` MCP or `personal-agent mcp` CLI. Both dispatch the typed
`IntegrationRequest` to the same plugin. The manager exposes list, inspect,
configure, enable/disable, remove, verify, apply and reload. Interactive OAuth is unsupported in the Pi adapter; login requests return an error without a fabricated URL.
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
  and fragments are rejected. Supply credentials through private environment/header references. Interactive MCP OAuth is not implemented by this adapter.

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
scratch for temporary downloads/scripts. Record the installed package version and
startup definition in the versioned registry and its adjacent installation notes.
Do not modify another agent installation’s global configuration. An existing installation outside `state/`
can be referenced by its actual executable path without copying package binaries.

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
ownership. Interactive OAuth is pending independent implementation and acceptance.
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
and actual stdio/HTTP MCP transports without inference. It discovers both configured scopes, dispatches
real synthetic tools without model inference, rejects excluded scope, refreshes
the same foreground thread, reports a missing executable, disables maintenance
tools and cleans up processes/threads. It touches no production registry, physical
device, user conversation or model API. Unit tests cover optimistic concurrency,
redaction, atomic-write failure, stale probes, bounded cancellation, scoped task
management, unsupported OAuth reporting and configuration rollback. Real vendor pairing,
OAuth authorization and device actions require that integration's own acceptance.
