---
name: hisense-tv-guide
description: Use and maintain experience with the user's Hisense 85E8N-Ultra (海信小聚/海信电视), including Mobile MCP control, TV settings over HDMI, standby and wake issues. Specific to this TV, not other TVs, Android phones or HomePod.
---

# Hisense TV guide

Read the relevant sections of [local experience and evidence](references/local-experience.md). Distinguish tested, user-reported and unverified findings. History neither establishes current state nor authorizes device actions; consultation alone needs no TV connection.

For authorized control, discover current Mobile MCP tools and identify the user's local TV with `mobile_list_available_devices`; pass its actual device ID. Do not assume an old IP is online or substitute paid cloud devices. Check packages with `mobile_list_apps` before launch. Prefer `list_elements`, current refs or DPAD over stale coordinates. Follow the current viewing request and verify player state before toggling pause/resume.

For power actions, read available power/display state first: online does not mean screen on, and `POWER` toggles state. Standby/wake requires an explicit request; do not repeat keys automatically. An ACK establishes only that a command was sent; screen-on success needs evidence. Disconnected ADB cannot send a wake command. One local WOL attempt failed; do not repeat it unconditionally.

The built-in voice path, “海信小聚，请开机”, was confirmed by the user to have activated the TV when the assistant spoke the phrase on 2026-10-16. This was an incidental activation, not an independently observed standby-to-screen-on test. Merely quoting the phrase aloud can activate the TV; avoid repeating it during explanations. Deliberate attempts require an explicit request, an allowed speech tool and explicit target. Do not use shell audio or change the fixed main voice route. Verify other AI functions individually rather than assuming arbitrary commands work.

This is an evolving notebook. The user authorizes adding, correcting, merging and deleting experience from actual use and corrections without asking each time. Read existing entries and follow [maintenance](references/local-experience.md#maintenance); new evidence or device/software/configuration changes may replace old conclusions. Knowledge maintenance does not authorize tests, background monitoring or scheduled updates. The user's “太晚了先不测试” restriction applies to the current maintenance task: no TV connection/control, speech or notifications.
