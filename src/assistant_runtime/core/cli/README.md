# CLI conversation entrance

`cli` is a core plugin certified for Linux and Windows (including Server), requiring
`conversation`. Background plugin startup never reads a terminal.

```text
pa use <existing-instance-directory>
pa
pa chat --session <ID>
pa status
pa stop
pa restart
```

The first connection creates an independent CLI conversation; later connections restore
the operator's last selected session. Other connected terminals keep their own selection.

`pa` without a subcommand means `pa chat`. `pa use` remembers the absolute configuration
path without modifying or starting the instance. Selection priority is explicit `--config`,
`PERSONAL_AGENT_CONFIG`, the remembered instance, the current instance directory, then the
existing XDG config. Explicit/environment/remembered selections that are missing or invalid
fail without falling back to another instance. With no selection, the CLI explains `pa use`
and explicit `pa init`; it does not create an instance automatically.

The private selection file is `%LOCALAPPDATA%/personal-agent/cli.json` on Windows and
`$XDG_CONFIG_HOME/personal-agent/cli.json` (default `~/.config`) on Linux. It contains only
the config path. Package identity, service names and instance data paths remain unchanged.
Personal Agent exports the `pa` command for independent installation and use.

| Command | Behavior |
| --- | --- |
| /new [title], /sessions, /switch ID | Create, list or select a conversation |
| /history | Show recent history, including results received while disconnected |
| /queue | List accepted waiting and active request IDs |
| /supplement text | Add explicit input to the active execution |
| /answer QUESTION_ID text | Answer the displayed pending question |
| /cancel [REQUEST_ID] | Cancel active execution by default; pass ID to cancel waiting input |
| /help, /exit | Show help or disconnect |

Ordinary text queues for a new turn. Answers and tool summaries stream through the existing
private local IPC; attachment results display their reference and local retrieval path.
Ctrl+C clears the pending terminal input. EOF, exit, switching or terminal loss do not
cancel accepted work. There is no model selector, batch protocol or automatic retry of
uncertain model work. Configure a model in the instance configuration console before sending.

Startup validates the existing config and plugin certifications. A private startup lock
serializes concurrent clients. A matching installed system service uses the existing manager;
otherwise the existing management process is detached with platform-specific process flags.
Windows creates no background window. The manager retains ownership of business/tool children.
No service installation, elevation, automatic instance creation or alternate-port fallback occurs.
Startup errors reference `state/configuration/manager.log` and `business.log`.
Use `pa stop` / `pa restart` to manage the background. `stop` stops the instance, whereas
`/cancel` inside chat cancels a turn. Service installation remains an explicit `pa service install`.

Run `scripts/verify_native_chat.py` explicitly outside the automatic parallel test worker:
it creates a temporary instance, starts two real CLI subprocesses with deterministic terminal
input, checks one surviving manager and reconnects after both clients exit, then cleans up.
`--installed` tests the noneditable installed package. This verifies process lifetime, not
interactive keyboard handling, OS logout, SCM installation or machine reboot.
