# Optional chat web entrance

The independent kernel management service owns the public address/port. Chat opens directly at `/` while this extension is enabled and ready; configuration is at `/config/`. Chat API and assets remain mounted at `/chat/`. Management streams HTTP, SSE, uploads and attachments to the worker's private loopback transport. No chat implementation is imported into management, and chat failure/restart never removes configuration/repair access.

Enable the web plugin and `plugin_settings.web.enable`. Public listening settings belong to management.toml. `plugin_settings.web.host` is loopback only; `plugin_settings.web.port` defaults to 0 (automatic local port). Both are advanced internal transport settings, not another public address. Existing instances must replace non-loopback `plugin_settings.web.host` with 127.0.0.1 before upgrading. No inference, conversations, tasks or credentials are reset.

Admin and chat logins remain separate. Chat cookies have Path=/chat/; administrator cookies are never forwarded to the worker. An instance-private frontend key from the operator control channel protects internal HTTP. Direct local-port access is rejected. Client identity and HTTPS scheme are forwarded only on this authenticated path; external X-PA headers are stripped. Trusted public_origin/proxy policy remains owned by management.

The extension owns all chat routes/authentication and UI assets. The app supplies an owner-scoped http_frontend resource (key, publish(loopback_host, port), withdraw()). It exposes the current endpoint only through operator IPC, never runtime health or model tools. Disable/close withdraws the endpoint; re-enable or hot listener replacement publishes the new one.

Frontend production assets are built with base=/chat/. All API, SSE and attachment URLs use the same prefix. The gateway does not retry submissions or buffer SSE. Provider execution/usage/request deduplication remain owned by existing conversation/execution services.

Chat message headers and response activity always use the configured assistant name,
including historical messages. Headers keep the existing message-channel badge and time,
without model avatars, names or descriptions. Loading the conversation does not fetch
the model catalog or avatar assets. Model IDs and route revisions remain the execution
identity; configuration model presentation and explicit task model details are separate.

Reply Markdown can place a registered image with `![caption](attachment:<id>)`.
Only images owned by that reply turn can be placed. Each placed image appears once
and opens through the conversation's existing authenticated media endpoint.
Other attachments stay in the reply gallery. External Markdown images use text
placeholders. Code blocks preserve literal syntax.

## Composer delivery

The same compact composer is used before and during execution. While an execution is
active, messages default to direct steering (`supplement`, or `answer` for a pending
question). A compact switch on the right shows only the selected behavior and toggles
between steering and queued messages with one click or tap. No popup is opened;
keyboard Space/Enter also toggles it.
The choice is stored in this browser's local storage, survives reload and execution
changes, and syncs across tabs. An idle conversation always starts a normal new turn.
Stopping execution is a separate action and never clears the draft or preference.

Pending inputs appear above the composer, one line each, with image counts and controls
for drag ordering, keyboard/touch up/down ordering, inline text editing and removal.
They enter the conversation timeline after dispatch; removed pending inputs retain their
audit record without appearing as sent messages. Editing keeps existing attachments and
notification references. Revision/order conflicts never overwrite another edit or retry a
mutation automatically. If an input starts executing during editing, the unsaved text
remains available to copy explicitly into the composer without replacing its draft.

Direct steering currently accepts text only. Images and notification references require
an explicit switch to queued delivery; neither unsupported input nor a failed steering
request is silently resubmitted as a new message. Existing server-side execution identity,
queue, cancellation and request-deduplication semantics are unchanged.

After `npm --prefix frontend run build`, run `python scripts/verify_web_composer.py`
(with optional `--browser /absolute/chromium`) for isolated routing, persistence,
keyboard, attachment and light/dark desktop/mobile layout acceptance. This check uses
synthetic local responses and does not contact model providers or production services.

## Web conversation links

The default `/chat/` entrance retains its owner password, cookie, fixed `web`
binding and existing API behavior. Optional links expose a single existing
conversation through the same interface. They do not rebind `web`, copy history,
start another Agent, enable memory or change model/tool configuration.

### Operator workflow

Log into `/config/`, then open **聊天网页 → 会话快捷链接**. Select an existing
conversation, or explicitly create an
empty one for independent verification. Choose **仅查看** or **查看并继续聊天**
and a lifetime. The secret link is shown only when created; subsequent lists
contain management metadata, not the secret. Revoke it from the same page.

The operator page uses the existing administrator-only
`POST /api/entrances/web` with `{action, arguments}`:

| Action | Arguments |
| --- | --- |
| sessions | optional `before` |
| create_session | `title` |
| links | empty object |
| create_link | `session_id`, optional `permission` (`read` / `chat`, default `chat`), `expires_in` (seconds, 0 until revoked, maximum 365 days) |
| revoke_link | `id` |

The public two-choice entrances below are intended for fixed NFC URLs. Existing
secret shortcuts remain supported with their original semantics; they are not
silently converted into device approvals.

### Fixed entrances: device approval or account login

**固定访问入口** binds an existing conversation to a trusted approval endpoint
(`namespace:device-id`), a public display title and a temporary-access permission.
Its `/chat/e/<id>/` URL contains no login secret. The title is public; do not put
private conversation contents in it. The destination and approver are immutable;
close the entrance and create another to change the binding.

The landing page offers:

- **申请访问**: persists a browser-bound request, shows a six-digit comparison
  code, and waits up to five minutes. The authenticated approval adapter decides
  approval/denial and supplies the duration. No browser request can specify the
  destination, approver, duration or grant permission. Approvals can be claimed
  once within 60 seconds. The lease starts at approval, not at claim or first
  message, and never slides on refresh, reconnect or repeated approval. Durations
  are integer seconds, 1..31,536,000; there is no implicit duration when omitted.
- **登录进入**: uses the existing single-owner Web access password (not the
  administrator password). It opens the same conversation, keeps normal Web
  features and tool policy, and uses the normal seven-day login expiry. It does
  not require device approval or inherit a temporary/read-only restriction.
  This does not introduce a multi-account system or indefinite login.

Conversation pages use separate `/visit/` and `/account/` paths. An expired or
closed temporary visit never upgrades itself merely because an owner cookie is
also present. Account entrance requires an explicit click/login at the landing
page. Neither path changes the default `web` binding or other tabs' drafts.
Browser status polling is bounded to pending requests; devices use the shared
event notification plus a bounded recovery query, not continuous polling.

Current PP code and firmware are unchanged. Independent checks simulate a
trusted approval adapter; actual device display/buttons require later adapter
and firmware integration. There is no public HTTP approval endpoint and no
administrator "simulate approval" button in the product.

### Access inventory and shutdown

The administrator page lists **当前授权访问**: temporary visits, account entrance
visits, owner Web logins and existing shortcut browser authorizations. It shows
the destination, method, permission, start time when available and effective
expiry. It never exports browser cookies, claim secrets, passwords or device
credentials. Close an individual visit to end only that authorization. Close a
Web account login to invalidate that login across its Web/entrance pages.
Administrator login remains separate and is unaffected.

Closing, expiring or revoking access terminates an existing SSE stream at its
next check and rejects subsequent reads/writes. It does not cancel already
accepted Agent work, revoke screenshots/downloaded content, or permanently ban
a valid owner from logging in again. Closing one shortcut browser authorization
does not revoke its reusable secret URL; revoke the shortcut itself to prevent
new exchanges. Closing a fixed entrance stops both ways into that entrance,
without deleting its conversation or changing other entrances.

Additional administrator operations at `POST /api/entrances/web`:

| Action | Arguments |
| --- | --- |
| entrances | empty object |
| create_entrance | `session_id`, `endpoint_id`, optional public `title`, `permission` (`read` / `chat`) |
| revoke_entrance | `id` |
| accesses | empty object; active authorizations only |
| close_access | `id`, `kind` (`temporary`, `account`, `account_login`, `link`) from the inventory |

### Trusted adapter contract

`api.conversation.ConversationAccessRequest` is an additive API 2 contract,
provided by Web. An adapter declares it in `uses` and derives `endpoint_id` from
its authenticated device identity, never from arbitrary browser/voice arguments.
Model-execution call scopes are rejected. The capability is not an Agent tool.

```python
await context.invoke(ConversationAccessRequest("pending", authenticated_endpoint))
await context.invoke(ConversationAccessRequest("decide", authenticated_endpoint, {
    "request_id": request_id,
    "decision": "approve",  # or deny, without seconds
    "seconds": device_selected_seconds,
}))
```

`pending` returns at most 32 bound requests (IDs, entrance, title, permission,
comparison code, creation and deadline), without claim/browser credentials.
`decide` accepts only `request_id`, `decision`, and approval `seconds`; it accepts
no conversation ID. A wrong endpoint, expired/cancelled request or conflicting
second decision is rejected. Exact repeats preserve the original expiry and
cannot reopen a consumed or closed grant. `web.access_requested` is a wake-up
event carrying endpoint/request identity; adapters query on that event and after
reconnect to recover dropped notifications. Web disabled/unavailable means this
optional feature is unavailable, not that ordinary terminal chat must stop.

Pending requests are limited to three per entrance, 128 globally, and six new
requests per minute per peer. Active entrance visits are limited to 256. Terminal
requests/expired visits are pruned after a day once no retained grant needs the
request. Backend records survive restart, with absolute deadlines and no message
replay. Owner-password rotation invalidates old pending/approved requests and
visits; the non-secret fixed entrance remains usable after a fresh login or
device approval.

### Authorization and lifetime

A link has the form `/chat/c/<public-link-id>/#<random-secret>`. The public ID
is a locator, not authority. The page erases the fragment before awaiting a
same-origin `POST /chat/c/<id>/api/link`. A successful exchange sets an HttpOnly,
SameSite=Strict browser cookie scoped to that individual entrance. Distinct
paths avoid both normal tab switching and simultaneous first-open cookie races.
The browser authorization lasts at most seven days; a valid reusable link can
authorize it again. Neither the secret nor the browser credential is stored in
localStorage/sessionStorage; the server stores their SHA-256 digests.

Each request verifies the grant's session, permission, expiry, revocation and
current owner-credential generation. Revocation/expiry also ends an existing
event stream at its next authorization check (normally within 0.5 seconds, apart
from an already ongoing network write). Revocation does not cancel accepted
Agent work. Logging out of one shortcut does not log out other shortcuts or the
normal Web entrance. Owner-password rotation invalidates shortcut authority once
the updated credential is applied. Restart preserves the records but never
replays a conversation input.

`read` allows history, state, events, registered images and recorded result
references; it rejects input, upload and conversation changes on the server.
`chat` additionally allows the existing submit, supplement, clarification,
queue, retry and cancel operations in that session. Supplied foreign session
IDs and foreign attachments/results are rejected. Global task/notification
management, fixed voice controls and administrator APIs are not granted. Task
reports already delivered into the conversation remain readable there.

Chat authority uses that conversation's existing Agent/tool permissions and
shared usage admission; it is **not** an OS or tool sandbox. Treat a reusable
secret URL like a key: copying it conveys its authority. The exchange does not
prove an NFC tap or physical device possession. HTTPS protects credentials and
content in transit; fragment erasure and hashing do not make HTTP encrypted.
The feature works behind the current management listener and does not require
the firmware site's port or a second business service.

### Core boundary and compatibility

The additive `conversation_views` resource exposes public `ConversationViews`.
Trusted declared entrance code calls `await views.open(session_id)` only after
authorizing the browser. The returned conversation handle binds history,
events, input, uploads and attachment access to one immutable selection; it
does not expose the host or another plugin's repository. A result reference is
readable only after it is found on a message in that view. Existing
`conversation` resources and `ConversationRequest` shapes are unchanged.

Persistence is additive and Web-owned: `web_links`, `web_link_browsers`,
`web_link_authorizations`, `web_entrances`, `web_access_requests` and
`web_entry_access`. Existing sessions, cookies and conversation records are
preserved. Web 1.2.13 remains within API 2 / core 0.4; no major version, new
Agent loop, PP change or production deployment is required by this addition.

### Independent checks

```sh
PYTHONPATH=src:tests .venv/bin/python -m unittest -q test_web_access test_web_access_native test_web_links test_web_links_native
npm --prefix frontend run build
```

The HTTP tests cover scope, read-only writes, media/result ownership, deduplication,
cross-session cancellation, concurrent browser entries, reusable links, expiry,
revocation, SSE shutdown, logout and owner-login independence. The native test
uses real management/business processes and Pi against a local synthetic model,
including restart and context continuity. It does not contact a paid provider,
read historical user conversations or access PP hardware. Access-specific tests
also cover simulated device approval, denial/cancel/deadlines, fixed lease
expiry, claim/decision deduplication, account policy independence, inventory and
administrative stream shutdown. Native tests run both modes against the same
real Pi context and verify restart without re-execution.
