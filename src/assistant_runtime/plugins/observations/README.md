# Observation sources

The optional `observations` plugin manages source instances; it never polls devices
itself. It depends on `events` for durable recording. Add both to the enabled plugin
list. Protocol providers are explicitly injected by the composition root; MCP input
cannot import arbitrary code. Other trusted plugins can report directly without this
management plugin.

## Ownership and configuration

`config/observations/sources.json` is the version 1 revisioned registry of producer
instances `{adapter, config, enabled}`. It belongs to source management, not the event
store. Addresses, polling intervals and credential references are producer configuration.
Credentials live under private `state/observations/credentials/` with mode 0600.
Source content remains in `state/events/`. Neither events nor registry files contain
passwords/session cookies. Registry updates use CAS and atomic replace/fsync.

The manager delegates `create`, `run`, `verify` and validation to registered providers.
It does not require an interval, a sample method, a snapshot or a retry event: a push
provider can await a subscription, and a business provider can register a callback.
`polling.py` is an opt-in helper used only by the router/TV providers; it is not a
framework scheduler. Each instance chooses its own cadence and backoff.

| Producer owner | Adapter | Configuration | Semantics |
| --- | --- | --- | --- |
| `assistant/wake_events.py` | `wake` | `{}`, one instance | Business acceptance hook and correlated voice connection result; no audio saved. |
| `plugins/reyee/` | `reyee` | `url`, `credential`, optional `interval` (30s, min 10s) | Complete wired/Wi-Fi client snapshots; disappearance confirmed twice. |
| `plugins/adb_tv/` | `adb_tv` | `address`, optional `interval` (5s, min 5s) | Read-only power/display; unreachable is unknown, never off. |

Sources have stable identities. Never reuse an old ID for a different logical source.
Updating only one instance stops/drains/restarts only that instance. Removing a source
retains historical records; disabling opens a gap. Background management requires an
active task with the `events_management` resource. Validation tests connectivity only;
normal physical transitions establish semantics separately.

## Add a producer

1. Keep a protocol-family owner outside `plugins/events` and `kernel`. Define a trusted
   provider with `description` (schema, event types/versions), `max_instances`, `validate`
   and `create(reporter, config)`. Register it in the composition root.
2. Its instance exposes `health`, async `run()` and read-only async `verify()`. Cleanup
   subscriptions/connections in `run`'s `finally`. Cancellation must stop observation.
   Do not use verification to power on devices or emit synthetic observations.
3. Choose push/business callbacks when available; otherwise the producer owns bounded
   polling. Use the bound reporter only; no event storage imports. A future HTTP/MQTT
   ingress authenticates external identities and translates payloads through this same
   contract. Such a network ingress is not implemented by this change.
4. Define stable keys, subjects, versions, time uncertainty, complete-snapshot checks,
   baselines, gap recovery and resource limits. Advance comparison state only after a
   committed checkpoint. Reuse unchanged identities/content after uncertain replies.
5. Test no interval/poll assumption, duplicates, partial/failed reads, outage recovery,
   storage/backpressure failure, reconfiguration and cleanup. Use isolated protocol
   fixtures first, then separately record physical connectivity/semantic acceptance.

## Instance files

The registry belongs to private instance `config/`, not Coding AI `.agents/`.
`config.example/observations/sources.json` is an empty reusable template.
Never overwrite an existing registry when copying examples; IDs, revisions and
credential references are meaningful configuration. There is no implicit discovery
or import from development tooling. Records and credentials keep their existing
plugin-owned storage, independent of configuration location.
