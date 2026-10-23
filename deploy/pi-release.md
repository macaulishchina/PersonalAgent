# Pi release and production cutover

The supported release entrance is Web text/images with configured model execution. Voice and OS
sandboxing are deferred. Fixed USB bindings remain unchanged. Old conversations and
plans are archived/reset, not resumed. Memory source transcripts, media, events,
credentials and physical files are preserved.

Production has one assistant instance and one model gateway. Cutover replaces the
existing service/configuration at the existing paths and ports. Backups and a
non-running candidate are files for preparation/recovery, not a second deployment.
Temporary acceptance hosts/containers are removed after testing.

## Preparation without touching production

1. Prepare a private candidate using the current `config.example/` schema. Preserve
   the real instance `root`; never copy empty example registries over live ones.
   Use `pi`, `models`, `ai_usage` and `voice_disabled`; remove retired executor fields
   and `model_api.execution_limits`. Metering belongs to `ai-usage.toml`.
2. Keep the chosen main model/effort explicit. Disabled providers are excluded from
   this release's live acceptance; provider availability is not a reason to re-enable
   them. Rebind affected main/task/memory uses explicitly before declaring readiness.
   Configure all four task profiles and
   accepted, ordered fallbacks. Memory requires an explicit structured-output model.
   `execution_validated` is a declaration, not proof of supplier availability or Pi
   compatibility; old Codex receipts do not establish new executor acceptance.
3. Validate without connecting to devices or model services:

   ```sh
   .venv/bin/python -m scripts.check_pi_release --config /private/candidate/config.toml
   .venv/bin/python -m scripts.verify_integrations
   .venv/bin/python -m scripts.reset_execution_state --root /absolute/instance
   ```

   Preflight reports configuration, local credentials, registries, skills and the
   pinned Pi version. It does not claim an account has quota or a provider works.
   The reset command without apply is read-only. Investigate unknown executions
   through their recorded tool receipts; never infer that they are safe to replay.
4. Run bounded real-provider checks only under the user's data/cost authorization.
   Use a separate instance root, private gateway key, loopback port and Docker project
   when regenerating gateway assets. Running the preparer with the production root
   writes production gateway inputs, even if its container is not restarted.

   ```sh
   .venv/bin/python -m scripts.verify_models --config /private/test/config.toml \
     --model ACCEPTED_ALIAS --case tools --max-requests 3 \
     --max-output-tokens 1024 --confirm-paid --output /private/tools.json
   ```

   Also check `text`, `image` and `memory` for the uses actually being enabled.
   For real-model delegation and shared metering, use `scripts.verify_pi_host`
   with `--main-model`, `--task-model`, `--max-requests 10`, `--confirm-paid`
   and a private `--output`. It creates a temporary host and no device configuration;
   the duplicate submission must produce one task and two server-bound callers.
   Vision must identify the supplied synthetic color without file tools; a generic
   “image received” answer is insufficient. Memory requires the actual structured
   format and source fields, with usage admission before dispatch. HTTP 429 or
   unsupported structured output blocks that use; do not quietly substitute a model
   or drop format constraints. Synthetic host tests cover mixed task identity,
   delegation, result ownership, fallback, cancellation and restart. Separately
   verify live-provider behavior; do not mark a missing supplier test as passed.
5. Finish review and `.venv/bin/python scripts/check.py` after the final code/test
   edits. Record the exact release commit, configuration revision, tested routes and
   limits. Keep credentials and receipts private. Stop/remove isolated test services.

   Build from a clean staging tree. Setuptools can retain removed modules in
   `build/lib`; an incremental wheel can therefore contain the retired executor even
   when the source checkout no longer does. Inspect the wheel for the native account
   helper, execution extension, bundled skills and Web assets; reject retired modules
   and private instance files. Extract it into a temporary directory and run preflight
   with imports resolved from that directory before installation. An uncommitted
   candidate and its wheel hash are preparation evidence, not a committed release.

The current private candidate binds the main conversation and memory to the accepted
native ChatGPT `gpt-6.1-sol` alias, with medium main effort. Task profiles retain
low/medium/high/max and four concurrent tasks. DeepSeek's accepted API route remains
an explicit fallback where capability and no-replay checks permit it. Go is disabled
and absent from active bindings/fallbacks. The native catalog advertises five Sol
levels (low/medium/high/xhigh/max); only low/medium have real-provider execution
evidence in this release preparation. Do not describe catalog availability as a test
of every effort or model. Gateway assets staged for inspection must be regenerated
at the real instance paths during cutover; staging is not a parallel deployment.

## MCP authentication boundary (optional future capability)

Configured stdio and HTTP/header authentication are supported. Interactive MCP OAuth
is not implemented in the adapter. Pi 1.4.0 offers native `pi mcp login`, but its
credentials and refresh locks live in the agent directory; this application isolates
that directory per execution. A temporary login alone would therefore not provide
reliable authentication for later foreground/background executions. Do not expose a
URL and claim completion without accepting shared instance credential ownership,
refresh, cancellation, restart and scope isolation end to end. Do not use a global
user agent directory or copy tokens between sessions as a shortcut. If a required
production integration needs OAuth, complete this capability before enabling it;
otherwise explicitly document the first release's configured-authentication scope.

No currently configured business requirement has been identified for interactive
MCP OAuth. `mobile_mcp` is local stdio. The configured HTTP `codex_resets` endpoint
successfully initialized and listed two tools without login during release review;
this was discovery only, not application-tool acceptance. Aliyun and Quark login
belong to their media adapters and are independent of MCP OAuth. The unimplemented
generic OAuth entrance is therefore not a release gate for this instance.

## Controlled cutover

1. Resolve model availability and authentication scope first. Inspect current runs,
   pending external/media operations and their latest receipts; cancel or reconcile
   deliberately. A service restart is not task cancellation.
2. Stop the service and confirm the process and in-flight tools have settled. Take
   the final stopped-instance backup/reset:

   ```sh
   .venv/bin/python -m scripts.reset_execution_state --root /absolute/instance \
     --apply --confirmed-stopped
   ```

   This backs up the database and private configuration before clearing old execution
   state. An earlier online snapshot is supplemental, not the final cutover backup.
3. Replace the existing release and configuration with the reviewed candidate in
   place. Reuse the current service unit, instance root and listening ports; do not
   leave the old assistant or gateway running alongside the new one. Generate the
   gateway inputs from that configuration, recreate the existing gateway explicitly and check
   local authenticated health. Run preflight against the installed configuration.
4. Start once. Check service readiness, Web login/text/images, a bounded one-shot
   task, auxiliary memory, configured MCPs, event collection, media/library readback
   and usage receipts. No device actions, bulk downloads or new recurring plans are
   implied by startup acceptance. Verify request/identity continuity after one clean
   restart once the first execution has fully settled.
5. Recreate only the plans the user still wants. Never auto-resume old plans or use
   a model fallback to bypass a metering denial.

## If startup acceptance fails

Stop new dispatch and retain the new release's receipts and unknown-operation state.
Fix configuration or the failing capability explicitly. Rollback requires the matching
old executable/environment and backup, not only an old TOML file. Never automatically
restart the old paid executor: that version contains the billing incident's runaway
behavior. Do not restore an old database over uncertain external actions or rerun them
under new IDs. Rollback and old task reactivation are deliberate maintenance actions.
Native model accounts are configured through `model_api.accounts` and operator-only
`model-accounts login`. They reuse Pi's provider library without adding a second
assistant or Codex process. Keep their private credential store independent of the
release workspace; never duplicate rotating OAuth tokens for an isolated test. Login
and model catalogs do not establish acceptance. Disabled Go providers remain excluded.
ChatGPT requires explicit `output_limit="model"`; its route cannot enforce the
gateway's small per-request output cap. Work request limits still gate every call.
