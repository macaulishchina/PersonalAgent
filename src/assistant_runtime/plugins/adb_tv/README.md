# Television observations

Host platforms: Linux and Windows / Windows Server, through the `observations` plugin. This protocol provider is not an independently enabled plugin. Actual router/TV connectivity requires separate acceptance.

The ADB protocol and power/display interpretation live here. Instance lifecycle and
configuration use [observation sources](../observations/README.md).

ADB executables resolve from the service account PATH (`adb.exe` on Windows); no caller-provided shell commands. Install the official Android platform-tools separately; the adapter does not install drivers or silently change its target.
No power key, WOL, voice, app launch or route switching occurs. Offline is a gap,
never inferred shutdown. Standby may be observable only briefly; a five-second poll
can miss it. Connectivity verification does not establish that transition semantics
were observed on the real device.
