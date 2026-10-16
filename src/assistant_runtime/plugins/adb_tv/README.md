# Television observations

The ADB protocol and power/display interpretation live here. Instance lifecycle and
configuration use [observation sources](../observations/README.md).

ADB executables resolve from service PATH; no caller-provided shell commands.
No power key, WOL, voice, app launch or route switching occurs. Offline is a gap,
never inferred shutdown. Standby may be observable only briefly; a five-second poll
can miss it. Connectivity verification does not establish that transition semantics
were observed on the real device.
