"""Install/enable this user's service. Requires no root and does not start it."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (ROOT / "config.toml").exists():
        shutil.copyfile(ROOT / "config.example.toml", ROOT / "config.toml")
    version = subprocess.check_output(["wireplumber", "--version"], text=True)
    if "0.4." in version:
        compatibility = Path.home()/".config/wireplumber/bluetooth.lua.d/90-dudu-audio-compat.lua"
        compatibility.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/"deploy/wireplumber/90-dudu-audio-compat.lua", compatibility)
        print("Installed Bluetooth SBC/44.1 kHz compatibility; takes effect on next WirePlumber restart.")
    else:
        print("WirePlumber is not 0.4; Bluetooth compatibility settings need migration before room testing.")
    node = shutil.which("node")
    paths = [str(ROOT / ".venv/bin"), str(Path.home()/".local/bin"), str(Path.home()/"miniforge3/bin")]
    if node:
        paths.append(str(Path(node).resolve().parent))
    paths.extend(["/usr/local/bin", "/usr/bin", "/bin"])
    unit = Path.home() / ".config/systemd/user/dell-voice-assistant.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(f'''[Unit]
Description=DELL personal voice assistant
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire.service pipewire-pulse.service wireplumber.service
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={ROOT}/.venv/bin/dell-voice run
Environment="PATH={':'.join(paths)}"
Environment="XDG_RUNTIME_DIR=%t"
Environment="PULSE_SERVER=unix:%t/pulse/native"
Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus"
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
UMask=0077

[Install]
WantedBy=default.target
''')
    env = os.environ.copy()
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    for args in [["daemon-reload"], ["enable", unit.name]]:
        subprocess.run(["systemctl", "--user", *args], env=env, check=True)
    print(f"Installed and enabled {unit.name}; start with dell-voice start")


if __name__ == "__main__": main()
