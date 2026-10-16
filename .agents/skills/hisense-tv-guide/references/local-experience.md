# Local experience and evidence

Compiled: 2026-10-15 (UTC). All states below are historical, not current connectivity or display status. IP/MAC information belongs to this local instance; the full wired MAC stays in the local evidence below, not this shareable skill.

## Maintenance

The user authorizes maintaining this notebook from actual use, test results and corrections without asking each time. Read relevant entries before adding, correcting, merging or deleting. Keep useful current conclusions, conditions, dates and sources; new evidence or device/software/configuration changes can replace old conclusions, including failures.

Distinguish **tested**, **user-reported** and **unverified**. Upgrade or correct status only with evidence. Failure to find a setting within an inspected scope does not prove permanent hardware limitations. Remove obsolete, wrong, duplicate or user-retracted guidance; retain brief historical qualifications only when useful for decisions, rather than accumulating a changelog. Do not delete unrelated raw test artifacts or user files. Maintenance does not authorize tests or background/scheduled observation; device actions still require current authorization and the no-testing restriction below.

## Identity and tools

- **Tested, 2026-10-15:** Hisense **85E8N-Ultra**, Android 11. `VIDAA_TV` is a generic model identifier, not the retail model. Mobile MCP then had authorized wired target `110.0.0.2:5555`; the TV's eth0 was `110.0.0.2/24`. **User-confirmed:** wired IP `110.0.0.2`. Enumerate local devices again before connecting.
- **Tested, 2026-10-15, evidence A:** `@mobilenext/mobile-mcp` **1.0.11**, successful initialize and tools/list, 33 tools. **Environment reported in the handoff:** Mobile MCP was natively loaded; the earlier installation report's “未热加载” described that earlier session. Discover actual tools rather than copying schemas or assuming reinstall/restart is necessary. Tool count does not establish TV support for every action.
- **Tested, evidence B/C:** the wired MAC came from the TV's eth0 and matched across checks, not a guessed neighbor-cache entry. Read B's `device-network-evidence.txt` for the full value. Historical broadcast `110.0.0.255` does not guarantee future routing.

## HDMI settings, apps and playback

Prior tested experience supplied in the user's handoff, compiled 2026-10-15. No separate playback acceptance file was supplied; this was not retested during compilation.

- From HDMI video, launching `com.android.tv.settings` exposed “图像设置” without returning home or changing input. Evidence B also records opening system menus from the signal screen. This controls the TV's Android system, not the external HDMI box. HDMI-CEC device coordination differs from Ethernet WOL.
- Tested apps: 影视仓 `com.huawei.himovceif`, system settings `com.android.tv.settings`, 当贝桌面 `com.dangbei1.tvlauncher`. Check current installation with `mobile_list_apps` before direct launch; use current elements/refs or DPAD, not old coordinates.
- Previously resumed 《现在就出发第四季》 from 影视仓 history. This is a past action, not a permanent preference; follow the current viewing request.
- In that player, `DPAD_CENTER` toggled pause/resume and `VOLUME_DOWN` lowered volume one step. Verify the current player/state first; do not generalize to other apps.

## AI voice path: user-confirmed activation

**User-reported, 2026-10-15:** built-in AI wake word “**海信小聚**”; the user says “**海信小聚，请开机**” can turn the TV on. **User-confirmed, 2026-10-16:** while the assistant explained and spoke this phrase, the user observed that the TV had already heard it and awakened. Accept this feedback instead of repeatedly claiming no activation occurred. This establishes user-observed voice activation, not an independently instrumented standby-to-screen-on test or support for all commands. Merely reading the phrase aloud can trigger the TV, so do not repeat it unnecessarily in spoken explanations.

The user originally said “太晚了先不测试”; this restricted that maintenance task, not all future requests. Deliberate attempts require a current explicit request and an allowed speech tool and target. Successful audio output alone does not prove the TV woke; distinguish the user's observed confirmation from tool evidence. Do not bypass tools through shell playback or changes to fixed main voice bindings.

## Standby, menu inspection and failed WOL

**Tested, 2026-10-15, evidence B/C:**

- `POWER` is a toggle, not idempotent power-on. The standby test verified Awake / Display ON, sent POWER once, then verified Asleep / Display OFF. ADB briefly remained available before disconnecting. Online does not establish screen-on; disconnected ADB cannot send a wake key. Only standby/wake on explicit request; no automatic repeated POWER presses.
- Within inspected network, 通用→开关机, AI and casting menus, no network wake/standby, quick boot or phone power-on option was found. Far-field voice was enabled in AI settings; this does not prove standby voice wake works. Help for “遥控器待机：直接待机” only explains whether pressing standby opens a standby-choice screen; it is not a WOL switch. These are scoped inspection findings, not permanent hardware limits.
- **One failed short-standby WOL attempt:** after verifying Awake, sent POWER once and verified Asleep / Display OFF. Sent one **102-byte UDP magic packet** using the verified wired MAC and then-correct broadcast route: host `110.0.0.82`, interface `enp1s0`, destination `110.0.0.255:9`. After about **31.713 seconds (about 32 seconds)**, endpoint networking and ADB had not recovered, Mobile MCP did not list the target, and screen-on was not established.
- sendto returning 102 bytes proves local submission only. Do not repeat identical WOL unconditionally or infer deep/long standby or cold-boot behavior from this single short test. Recovery in that recorded state required the remote, or future verification of the voice path; this is not a live state assessment.

## Local evidence index

Existing files dated 2026-10-15 (UTC); read only relevant content, without accessing devices:

- **A — installation acceptance:** [mobile-mcp-install-report.md](/home/macaulish/workspace/dell-voice-assistant/state/work/tasks/dcbcee82e88e443f9ca76ae3c6ab84df/e2c84fdf2ac84e3ea4f90c488365f14b/artifacts/mobile-mcp-install-report.md), verified at 17:42:50 UTC.
- **B — model, menus and wired identity:** [wake-check.md](/home/macaulish/workspace/dell-voice-assistant/state/work/tasks/102cbfc6e7f6475683d2a78e11c0f36e/31234fc833fe489ebc0364118e368e2c/artifacts/wake-check.md); [device-network-evidence.txt](/home/macaulish/workspace/dell-voice-assistant/state/work/tasks/102cbfc6e7f6475683d2a78e11c0f36e/31234fc833fe489ebc0364118e368e2c/artifacts/device-network-evidence.txt) contains the full wired MAC; read locally only as needed.
- **C — standby WOL test:** [wake-test.md](/home/macaulish/workspace/dell-voice-assistant/state/work/tasks/1ab09a327ffb4727990bedbdbc0ab879/b8a5d36ea83344beb449125a6b01dacf/artifacts/wake-test.md), tested at 18:05–18:06 UTC (Beijing time October 16, 02:05–02:06).
- **User statements/handoff:** task messages dated 2026-10-15, covering wired IP confirmation, AI wake word/command, loaded environment and app/playback test summaries. These statements were not upgraded to live verification.
