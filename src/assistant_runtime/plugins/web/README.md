# Optional chat web entrance

The independent kernel management service owns the public address/port. Configuration stays at /; chat is mounted at /chat/ only while this extension is enabled and ready. Management streams HTTP, SSE, uploads and attachments to the worker's private loopback transport. No chat implementation is imported into management, and chat failure/restart never removes configuration/repair access.

Enable the web plugin and web.enable. Public listening settings belong to management.toml. web.host is loopback only; web.port defaults to 0 (automatic local port). Both are advanced internal transport settings, not another public address. Existing instances must replace non-loopback web.host with 127.0.0.1 before upgrading. No inference, conversations, tasks or credentials are reset.

Admin and chat logins remain separate. Chat cookies have Path=/chat/; administrator cookies are never forwarded to the worker. An instance-private frontend key from the operator control channel protects internal HTTP. Direct local-port access is rejected. Client identity and HTTPS scheme are forwarded only on this authenticated path; external X-PA headers are stripped. Trusted public_origin/proxy policy remains owned by management.

The extension owns all chat routes/authentication and UI assets. The app supplies an owner-scoped http_frontend resource (key, publish(loopback_host, port), withdraw()). It exposes the current endpoint only through operator IPC, never runtime health or model tools. Disable/close withdraws the endpoint; re-enable or hot listener replacement publishes the new one.

Frontend production assets are built with base=/chat/. All API, SSE and attachment URLs use the same prefix. The gateway does not retry submissions or buffer SSE. Provider execution/usage/request deduplication remain owned by existing conversation/execution services.
