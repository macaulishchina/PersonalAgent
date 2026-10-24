# HID function buttons

Platform: Linux only; Windows / Windows Server are not certified. See the [module index](../../../../docs/reference/modules.md).

Optional `hid_buttons` plugin translates explicitly configured USB HID reports into
existing `Trigger(source, "wake")` requests. The assistant retains wake acceptance,
quiet hours, connection and feedback ownership. No kernel or audio binding changes.
This is a physical local entrance, not a model tool or a general shell-command mapper.

## Configuration

Include `hid-buttons.toml` from the configuration entry and enable `hid_buttons` in
`plugins`. See `config.example/hid-buttons.toml`. Each mapping requires vendor ID,
product ID, serial, exact report bytes (including report ID) and `action="wake"`.
At most eight devices, one mapping per identity; debounce defaults to 500 ms.
The plugin resolves sysfs identity on each connection, never a fixed hidraw number.
Zero or multiple matches fail closed. It opens only read-only, nonblocking descriptors;
no feature/output reports, audio changes or grabs. Volume and mute reports are ignored.

Reads begin after runtime readiness. Each callback processes at most 32 reports.
Each source has at most one outstanding wake request, with no backlog or replay.
Matching reports within the debounce window are ignored. A failed/uncertain wake is
not retried. Missing devices and permissions degrade only this plugin and retry every
2 seconds. Shutdown removes readers, closes descriptors and cancels owned work.
`status` plugin health exposes degraded reasons; logs contain mapping names and wake
acceptance, never raw reports or audio. An accepted wake is not a successful voice
connection; the existing wake lifecycle records connection results separately.

## Linux permissions

The service user needs read access to only the selected device's hidraw node.
For this installation, `deploy/70-personal-agent-mcp01.rules` grants read access to
members of group `macaulish` for USB ID `17ef:a03b`, serial `ACTIONS-DEV-SN1`.
Review/change the group and identity for another installation. Install with:

```sh
sudo install -m 644 deploy/70-personal-agent-mcp01.rules /etc/udev/rules.d/70-personal-agent-mcp01.rules
sudo udevadm control --reload-rules
```

The rule applies at the next device add/change. To apply it to a connected device,
first confirm its current node with `udevadm info --name=/dev/hidrawN`, then run
`sudo udevadm trigger --action=change /sys/class/hidraw/hidrawN` and
`sudo udevadm settle`. Use the actual node, not a persisted hidraw number.
Check node read access and plugin readiness after applying the rule. Existing
temporary read ACLs permit immediate use without unplugging the active audio device. No broad input-group access,
world-writable HID nodes or administrator privileges for the assistant are required.

## Evidence and acceptance

MCP01 USB `17ef:a03b`, serial `ACTIONS-DEV-SN1`, was observed producing `01fe`
for its phone/function key. One ten-press trial produced nine reports; do not claim
perfect touch detection or add synthetic triggers for missing input. Volume keys sent
bursts of duplicate reports. The hardware mute key changed red/green LEDs without
an observed HID report; the standard mute LED control did not visibly change the ring.
Do not infer microphone mute from this plugin or repurpose the privacy indicator.

Automated tests use synthetic reports, identities and sockets. Deployment acceptance
requires a real key press with microphone physically unmuted (green), checking one
accepted wake, correct fixed USB input/output, voice connection, and no extra trigger.
Also verify disconnect/reconnect and permissions after rule installation when the
user can interrupt audio. No automatic tests operate physical hardware.

Instance acceptance on 2026-10-22 (Asia/Shanghai): hardware replug was detected;
a missing permission degraded only this plugin while USB audio recovered. After
applying the installed udev rule, the node became `root:macaulish 0640` and the
listener recovered without a service restart. Green-light function-key presses
produced accepted HID wakes and successful native voice connections. The user
observed no wake with the hardware red mute indicator. This is evidence about this
MCP01, not a generic software mute-state check. Quiet hours were temporarily delayed
for testing, then restored to the original 01:00–06:00 policy and confirmed active.
