# System configuration

The configuration console is an operator capability, independent of the Agent and its
model tools. Both deployment presets start it through the same management entry point.
Production activation is a separate operation; development and tests never install or
restart the current production service.

## Use

```sh
pa init /absolute/instance --preset slim # or standard
pa --config /absolute/instance/config/config.toml run
pa configuration token --management /absolute/instance/management.toml
pa configuration reset-token --management /absolute/instance/management.toml
```

Open port **8047**, listening on `0.0.0.0` by default. The random administrator token is
private at `state/configuration/admin-token`; it is separate from the chat login.
`configuration serve --management ...` is the explicit management entry point. `run`
resolves the same bootstrap before parsing business configuration, so damaged business
TOML does not prevent the repair page from starting. Initialization does not start services.

After login, **修改密码** sets a custom administrator password (7–256 characters,
without leading/trailing whitespace, line breaks or NUL). Digits alone, letters alone,
or a mixture are accepted, with no character-class requirement. It verifies the current password
and repeated new input, shares the login rate limit, and atomically updates the private
credential. This account operation takes effect immediately, invalidates every administrator
session and requires login again; it does not restart the business process or depend on valid
business TOML. Only the action is audited, never the passwords. CLI `reset-token` remains the
local recovery path and generates a new random credential. Configuration restore does not
restore administrator passwords.

The console starts with an overview and setup progress. Model configuration follows
connections → model library → usage, with card lists, guided additions and an editor drawer.
Native connection creation stages the account, provider and private credential together;
OAuth login stays an explicit operation after saving. Low-frequency maps/lists retain bounded structured editors.

`static/connections.js` owns purpose-built connection and authentication forms shared by add/edit.
Preset definitions come from the models owner. Serialization is exclusive: preset, custom API,
or native account; never persist all generic schema fields together. Managed keys get internal
random references; advanced reference mode is mutually exclusive with entering a key.
Preserve existing references, isolate replacement of shared credentials, and use the common
configuration/resource draft transaction. Changing presets to a different endpoint clears the
selected draft credential, without deleting any saved credential.

Connection cards and drawers expose explicit current-draft connection checks. The add-model
wizard queries a searchable, bounded directory and copies a selected ID into the draft;
manual IDs remain available. Changing the connection invalidates pending UI results.
`POST /api/provider-query` uses the administrator session/origin checks and revision guard.
It accepts only the selected connection/account draft and an optional transient API key;
missing keys resolve through the selected reference using the normal environment/file precedence.
Draft keys are used only for this request and never persisted or echoed. No unrelated drafts
are validated. Owner validation maps to safe field-specific errors; the browser focuses the
field and rejects results if inputs change in flight. Testing does not save, enable, apply or
mark capabilities accepted. Native login remains an explicit account management operation.

Extension rows display Linux/Windows certification from packaged `plugin.json`,
without importing runtime code. Windows includes Windows Server. Missing or invalid
metadata is shown as uncertified. An incompatible checked entry can be unchecked,
but cannot be re-enabled. Saving, applying and startup enforce the same policy;
the console remains available to repair an incompatible existing selection.

The console uses purpose-based navigation and a persistent save bar. Optional capabilities
use cards, with core assembly under advanced settings. Desktop model/connection lists are searchable; radio cards explain connection routes.
Model tabs share a draft; cancelling
a drawer restores its checkpoint without discarding other edits. Switches, enum choices,
ordered reference lists and structured JSON keep their original value types.
`workspace.js` composes these first-party views over the shared typed editor and transaction
client in `console.js`; extension pages still use packaged interface declarations. Technical field/file/default
information is available on demand. Apply/restart, history, repair and password changes
open separate dialogs instead of occupying the main form. Page navigation offers stay, discard, or save-and-switch; a failed save keeps the page and
draft. Malformed inputs show local accessible errors and block saving. Percentage presentation
converts to the original stored fraction; saved files remain separate from loaded values.

**Edit → save and apply → inspect the outcome.** Save preserves comments,
explicit includes and unrelated fields; defaults are displayed, not materialized by an
unrelated edit. Reset removes an override. The page distinguishes file and loaded values.
Saving does not start inference, install software or restart the worker. Applying an enabled
connection can replace that owner's connection locally; live work first reaches a safe boundary.

`ConfigurationService` is shared by CLI checks and management. The worker coordinates
`ConfigurationUpdate` preparation, commit, reverse rollback and cleanup. Field policies are
static owner data (`application`); UI mapping is independent (`interface`). Budget/parallelism
changes preserve usage and active leases. Model changes preserve logical sessions and Pi
context. Resource manifests use scoped candidate views and publish only after commit.
Failed candidates are blocked from automatic restart retry; worker receipts reconcile lost
IPC responses. Unrecoverable owners and their dependents refuse new requests. Management
listener failures remain separately pending and can be retried without restarting the worker.

Plugin composition, process foundation, fixed main voice bindings and storage migration
remain explicit restart/maintenance operations. A page is applied atomically: if its related
changes require restart, the candidate waits as a whole. External gateway steps stay pending
until the operator explicitly confirms them. Manual edits are detected but never auto-applied.

## Files and credentials

Application and owner `ConfigurationSpec` descriptions cover all current Config fields.
The public API and initialization/settings code share the shipped defaults. Startup and
save use the same owner semantic validators in addition to JSON Schema. Local schema
references are bounded and acyclic; remote references and frontend code are unsupported.

Registered resources include `.env`, management bootstrap, private gateway key, web access
credential, revisioned MCP registry, explicitly selected instance skills and their bounded
assets, and selected/configured external plugins' declared private resources. Resource
files outside the instance are read-only and require local maintenance. Instance/install
path changes are not data migrations and remain local operations.

Secret responses expose configured/source state only. Replace/clear are explicit actions;
external environment values cannot be changed through a file edit. MCP registry replacement
is write-only because existing transport arguments/headers may contain secrets; its owner
normalizes definitions and increments revisions. Use credential references. Resource and
TOML changes can share one transaction. Plugin JSON resources may carry their own schema.
Skills remain explicitly selected, with relative assets retained in the frozen generation.

A cooperative instance lock, content version, complete private backup and fsynced rollback
journal protect saves. A failed write restores all registered files, including untouched
credentials. Readers reject an incomplete transaction until management recovers it. MCP
writers share the lock and revision checks. Raw local edits are detected by content hashes.
Backups retain the most recent 20 versions plus the loaded and last confirmed versions.
Restoring configuration does not roll back business data or replay work.

Workers read immutable TOML/resource generations, including lazily read private credentials
and declared extension resources. New executions also retain the loaded MCP definitions.
A saved MCP change applies at a verified idle boundary through the registry owner. Explicit MCP
verification may inspect a pending definition; it does not execute its tools or replay work.
Only declared MCP environment references are supplied from the loaded credential generation.
The independent process retains at most 20 generations plus any currently protected one.

Native account login/status/catalog/logout reuse Pi's isolated account helper. Login and
logout require confirmation and take immediate effect; browser prompts are transient,
bounded and cancellable. Authorization answers, callback URLs and tokens are not audited.
OAuth refresh storage is never included in configuration restore. Other plugins retain
their existing account-login and business-resource management interfaces; the console does
not expose their private business databases as configuration documents.

## Apply and process ownership

`app/management.py` owns lifecycle; this module owns operator UI, schemas, transactions
and audit. The business process has no configuration-management model tool.

- Wait pauses new roots and schedule dispatch, retaining preparation, accepted children,
  clarification, cancellation and finalization. Owner hooks provide bounded blockers.
  Waiting has no deadline that silently kills work and can be withdrawn before restart.
- Force requests cancellation, then ends only owned processes within the configured
  shutdown period. Unknown external outcomes are never declared undone or replayed.
- A Linux subreaper guardian detects manager pipe loss and cleans descendants even when
  they created separate sessions. Unexpected worker failure leaves an unclean marker;
  queued immediate task plans are reconciled rather than automatically launched.
- Startup verifies the guardian identity, loaded version and critical readiness. Failed
  business configuration, absent model login and plugin failure leave management reachable.
  There is no business restart loop. Optional failures are reported as partially applied.
- Application receipts and pending external deployment steps survive manager restart.
  Gateway configuration **or credential** changes require an operator deployment step;
  another successful worker restart cannot clear that step. Confirmation is version-bound
  and does not run container commands or establish provider acceptance.
- Management listener changes bind the new endpoint before retiring the old one. Failed
  binding preserves the working endpoint and an explicit outstanding step. Root/entry
  relocation is forbidden. The installer manages one unit and the owned process tree.

The bootstrap supports `host`, `port`, `startup_seconds`, `shutdown_seconds`,
`trusted_proxies` (CIDRs) and `public_origin` (HTTP(S) origin without trailing slash).
Behind TLS termination, set both the explicit public origin and the proxy networks.
Untrusted forwarded headers are ignored. Administrator sessions, strict origin checks,
SameSite cookies, no-store responses and CSP are independent of chat authentication.

Repair accepts replacements only for registered configuration files, validates the complete
candidate and detects conflicts without returning damaged contents that could contain
secrets. On the first startup, if even the entry/include list is unreadable and no prior
registration exists, restore that include list locally; the page never scans arbitrary files.
A damaged management bootstrap or administrator credential requires local CLI repair/reset.

## Validation boundary

Automated tests cover defaults/conditions/references, static discovery, secrets, conflict,
disk failure and journal recovery; native-account prompts/cancel; real manager/guardian
processes, frozen MCP and resources; drain/withdraw/force/restart, queued-task reconciliation,
and management availability with broken business config. Browser and installed-wheel
acceptance use private temporary instances, local synthetic endpoints and unused ports.
These checks do not assert new provider compatibility, complete a real user's OAuth login,
perform external media/device actions or authorize production cutover.

For isolated desktop browser acceptance, use an environment containing the application,
Playwright with Chromium, and the independently installed `examples/sample-plugin` package:

```sh
python scripts/verify_configuration_console.py
# To check a non-editable release environment instead of repository source:
python scripts/verify_configuration_console.py --installed --python /absolute/release/venv/bin/python
```

`--browser` selects an existing Chromium executable; `--artifacts /private/directory` saves
synthetic screenshots. The check creates a temporary slim instance, keeps model routes
disabled, and verifies 1280/1440/1920 desktop widths, external-plugin forms, typed values,
new references, removal, save/apply, repair and custom passwords. It does not load the
production instance, credentials or task plans. Install validation dependencies separately
from a running service. Mobile layout optimization is outside this desktop pass.

Historical results are preserved in the [module acceptance archive](../../../../docs/archive/module-acceptance-2026-10.md).

Model editing uses `model-editor.js` over the same typed draft/transaction controls. `/api/model-capabilities` is authenticated, revision-checked metadata-only lookup, independent of credentials or inference. Model facts and acceptance are separate; the page edits one set of model parameters and thinking settings. Adopting or refilling metadata replaces known fields directly in the draft; absent facts preserve the corresponding settings. Provenance is informational, not a parallel override form. Model/connection changes discard stale page metadata and require re-identification; server validation rejects stale saved bindings. No provider credentials are sent to public catalogs.

Public capability references use a searchable dropdown with all matching records across all packaged models.dev suppliers. Names are ranked by normalized exact match, substring and search terms; suggestions never change a route or grant capabilities. The operator previews a selected record and explicitly copies its known values into the normal configuration draft. Unknown models remain savable. The dropdown reuses the collection filter and model-list styling; filtering is local, selection explicit, and no pages are hidden. Searches are local, authenticated and revision checked, and require neither credentials nor inference calls.
