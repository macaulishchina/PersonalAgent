# Apple Home environment integration

The optional `apple_home` adapter registers temperature/humidity endpoints through the existing hardware onboarding standard. It supports sensor-equipped HomePod mini / HomePod (2nd generation) when those measurements are visible in Apple Home. Existing AirPlay/RAOP does not expose these measurements; the AirPlay speaker adapter remains separate. No HomePod firmware, Apple account credentials or receiver pairing are changed.

Apple Home owns sensor access. A Home automation reads the real accessory state and POSTs it to this adapter. Upstream HAP-python 5.0.0 supplies an encrypted, paired virtual HomeKit bridge with one **Lightbulb** refresh trigger per endpoint. Turning that trigger on runs the Home automation; it resets off after one second. The assistant polls it and can request refresh during `inspect_device`. This needs one-time Apple Home setup; it is not automatic Linux access to HomePod sensors.

## Install and declare

Use the project's existing environment and retain all other enabled extras:

```sh
uv sync --locked --extra voice --extra memory --extra web --extra airplay --extra camera --extra apple-home --extra dev
```

Add `apple_home` to `plugins`, and a top-level settings table before device tables:

```toml
apple_home = {host = "192.168.1.10", port = 8047, hap_port = 51827, poll_seconds = 60, refresh_timeout = 6}

[devices.homepod_environment]
adapter = "apple_home"
kind = "sensor"
label = "HomePod 温湿度"
location = "客厅"
physical_id = "living_homepod"
max_age_seconds = 180
[devices.homepod_environment.connection]
metrics = ["temperature", "humidity"]
trigger_name = "读取HomePod温湿度"
```

Set the existing AirPlay endpoint's `physical_id` to `living_homepod` too. These are two functional endpoints of one physical device, with separate owners/availability. One unavailable sensor does not disable playback. The same association also applies to camera/audio endpoints on a USB camera. Neither logical identity nor HomeKit accessory identity depends on registration order.

Use an explicit reachable LAN IP. Apple Home devices/hub must reach the HTTP ingest and HomeKit ports; HomeKit discovery uses mDNS. Neither this adapter nor installation changes the firewall. To enable the feature restart the existing service. Pairing state, secret ingest token and latest readings are private instance data. Do not serve `state/` through a static website.

## One-time Apple Home setup

1. Open `<instance-root>/state/apple-home/pairing-code.txt` locally. In the iPhone/iPad Home app choose **Add Accessory → More Options**, select **环境读数桥接**, and enter that setup code. The refresh accessory is a virtual light, not a replacement for the actual HomePod. Leave the actual HomePod paired as it is.
2. In **Automation → Add Automation → An Accessory Is Controlled**, choose **读取HomePod温湿度 → Turns On**. For the action choose **Convert to Shortcut**. This is a **Home automation**, running on the home hub, not an iPhone personal automation.
3. Add two **Get the state of Home** actions: select the real HomePod's **Current Temperature** and **Current Relative Humidity**. Keep those outputs as separate variables. Add **Current Date → Format Date**, using **ISO 8601** (including timezone).
4. Add **Get Contents of URL**. URL: `http://SERVER_LAN_IP:8047/readings`. Method: **POST**. Headers: `Authorization: Bearer TOKEN`, where TOKEN is read locally from `state/apple-home/ingest-token.txt`; `Content-Type: application/json`. Request Body: **JSON**, with these four entries:

   | Key | Value |
   | --- | --- |
   | `device_id` | Text: `homepod_environment` |
   | `observed_at` | Formatted date variable |
   | `temperature` | Real temperature action output |
   | `humidity` | Real humidity action output |

   Insert the action variables, not their names as literal text. Numeric values or measurement strings such as `24 °C` and `46%` are accepted; Fahrenheit is converted. Missing/calibrating data is not replaced with a plausible number. If Home's variable selector omits an action output, use a Text/Set Variable action to retain it before constructing the JSON.
5. Save and test the automation. Inspect the endpoint below and compare both values with Home. Leave the bridge enabled; the periodic trigger will then refresh without Windows or an open remote terminal. Verify refresh with the phone locked too. Pairing alone does not establish acquisition success.

No shortcut file is supplied: a Home automation must refer to the user's actual Home/accessories, and importing an unsigned shortcut is not a reliable substitute for configuring this Home automation.

## Contract, freshness and failure

The only HTTP route is authenticated `POST /readings`. It accepts only configured device IDs/metrics plus `observed_at`; it cannot execute assistant tasks. Input is bounded to 8 KiB. Whole reports are validated before replacing state. Non-finite, malformed, out-of-range and unknown values fail; duplicate/older source timestamps do not renew freshness. Structured measurement values also accept `{"value": null, "status": "calibrating"}` or `unavailable`/`error`.

Measurements use the stdlib `SensorReading` contract, exposed under each device's `readings` map with value, unit, source, sample timestamp, status, age and freshness. Device heartbeat age is separate from measurement age. Temperature can be current while humidity is calibrating. In-process monotonic ageing prevents a backwards wall-clock adjustment from rejuvenating old samples. Restoring state preserves the original sample time; an expired value stays explicitly stale. Only the latest readings are stored in SQLite; there is no history service, passive wake, automatic memory capture or unsolicited AI action.

Refresh requests coalesce per endpoint, enforce a five-second minimum interval, and wait at most the configured 1–8 seconds. Background refresh is 30–3600 seconds (default 60). Timeout returns the existing age/validity, never a fabricated successful reading. The optional source retries bridge/listener failures and reports degraded health without stopping voice, tasks or another endpoint. HomeKit keys/IDs survive service restarts; adding another endpoint does not reassign existing accessories.

## Query and acceptance

```sh
personal-agent --config config.toml devices list
personal-agent --config config.toml devices inspect homepod_environment
.venv/bin/python scripts/verify_environment.py --config config.toml --device homepod_environment --codex
```

Device MCP needs no HomePod-specific tool: `list_devices` discovers `sensor.temperature` / `sensor.humidity`; `inspect_device` requests refresh and returns typed readings. The model must inspect each metric's validity/freshness before stating a current value. The acceptance entry uses an isolated provider thread with the installed device socket; it does not play, capture media, change output, save memory or disturb the shared conversation. Evidence is private under `state/acceptance/environment/`. `--restart` additionally checks installed-service recovery without replaying a user task. Synthetic acceptance can check the HTTP → directory → real stdio MCP → Codex path with `--synthetic`; that is expressly not real HomePod success.

Primary references: [Apple HomePod sensor documentation](https://support.apple.com/en-gb/guide/homepod/apd8085c1032/homepod), [upstream Home automation readback pattern](https://github.com/gregbny/homepod-temp-logger/blob/main/TUTORIAL.md), [HAP-python](https://github.com/ikalchev/HAP-python).
