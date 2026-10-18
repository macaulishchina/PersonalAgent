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
apple_home = {host = "192.168.1.10", port = 8047, hap_port = 51827, poll_seconds = 60, refresh_timeout = 10}

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

Use an explicit reachable LAN IP. Apple Home devices/hub must reach the HTTP ingest and HomeKit ports; HomeKit discovery uses mDNS. Neither this adapter nor installation changes the firewall. To enable the feature restart the existing service. Pairing state, setup code, secret ingest token and latest readings are private instance data. The existing `pairing-code.txt` is reused after restart; read it directly instead of copying a code into another guide. Do not serve `state/` through a static website.

## One-time Apple Home setup

1. Open `<instance-root>/state/apple-home/pairing-code.txt` locally. In the iPhone/iPad Home app choose **Add Accessory → More Options**, select **环境读数桥接**, and enter that setup code. The refresh accessory is a virtual light, not a replacement for the actual HomePod. Leave the actual HomePod paired as it is.
2. In **Automation → Add Automation → An Accessory Is Controlled**, choose **读取HomePod温湿度 → Turns On**. For the action choose **Convert to Shortcut**. This is a **Home automation**, running on the home hub, not an iPhone personal automation.
3. Add two **Get the state of Home** actions: select the real HomePod's **Current Temperature** and **Current Relative Humidity**. Keep those outputs as separate variables. Add **Current Date → Format Date**, using **ISO 8601** (including timezone). If the output omits the timezone, choose **Custom** and use `yyyy-MM-dd'T'HH:mm:ssXXX`; the formatted result must end in an offset such as `+08:00` or `Z`. Insert that formatted output into `observed_at` below.
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

### Diagnose readback and automatic refresh

The receiver logs HTTP source/status, a report ID, failing field and fixed validation
reason, without request URLs, headers or complete payloads. A `400` means authentication
passed but the JSON/sample failed validation; the returned `error`, `field` and
`report_id` identify the rejection. This distinguishes a rejected report from a later
refresh timeout; neither implies an Apple-side action caused the malformed value.

For an authorized diagnosis, set `apple_home.log_invalid_values=true` in the instance
configuration and restart the service. Only rejected, authenticated reports for a
configured device get value diagnostics: source `observed_at` and configured metrics,
with original JSON-decoded type, presence, collection/string length and a maximum
256-character escaped preview per field. The ingest token is redacted even if it
appears as a sensor value; unrelated body keys are excluded. These private journal
entries help distinguish strings, arrays and structured values without changing
validation or accepting partial readings. The option defaults to false; disable it
after diagnosis to stop storing value previews. Successful reports are not dumped.

A successful manual test establishes readback, not unattended acquisition. Save both
the shortcut editor and the outer automation page, then verify new source timestamps
without tapping Test.

`inspect_device` includes `data.refresh_trigger.subscriber_count`. Zero means no controller currently subscribes to that trigger's changes; pairing alone is insufficient. A positive count and the `HomeKit refresh requested` log establish a subscribed trigger, not successful automation execution. When no POST follows, inspect the automation's enabled condition and the active hub under Apple Home settings. HomePod or Apple TV may be the active hub. Apple's [hub troubleshooting](https://support.apple.com/en-us/102557) includes restarting that hub; receiver success must still be verified afterward.

The same diagnostic object reports `controller_write_count`, `last_controller_write_at` and `last_controller_write_value` for inbound trigger-lamp control. These counters reset with the bridge process; internal polling pulses do not increment them. A controller write after an isolated refresh can establish execution of a Home-control action before a failing network action. It is not a sensor report and never renews measurement freshness.

Hub software is part of acceptance. A [first-hand OS 27 reproduction report](https://forums.macrumors.com/threads/significant-bug-in-home-automations-using-shortcuts-that-require-network-connection.2486785/) describes network actions failing silently on the hub while the same automation succeeds in an iPhone manual test. This is a reported compatibility issue, not an Apple-confirmed diagnosis for every installation. When investigating, record the advertised/installed hub version, test a network action before sensor reads, and distinguish iPhone-origin requests from hub-origin requests. Remove temporary diagnostic actions afterward. Do not claim unattended support based on pairing, subscriptions, manual POST success, or synthetic tests; require fresh automatic source timestamps. The same [report’s follow-up](https://forums.macrumors.com/threads/significant-bug-in-home-automations-using-shortcuts-that-require-network-connection.2486785/page-2) includes multiple October 6–7, 2026 user confirmations that URL GET/POST worked again with an Apple TV on tvOS 27.2 beta 3 as the active hub. These are user tests, not a guarantee for a particular installation or a recommendation to install beta firmware. Keep the adapter enabled with bounded timeouts while awaiting hub recovery; verify actual new automatic samples afterward.

## Contract, freshness and failure

The only HTTP route is authenticated `POST /readings`. It accepts only configured device IDs/metrics plus `observed_at`; it cannot execute assistant tasks. Input is bounded to 8 KiB. Whole reports are validated before replacing state. Non-finite, malformed, out-of-range and unknown values fail; duplicate/older source timestamps do not renew freshness. Structured measurement values also accept `{"value": null, "status": "calibrating"}` or `unavailable`/`error`.

Measurements use the stdlib `SensorReading` contract, exposed under each device's `readings` map with value, unit, source, sample timestamp, status, age and freshness. Device heartbeat age is separate from measurement age. Temperature can be current while humidity is calibrating. In-process monotonic ageing prevents a backwards wall-clock adjustment from rejuvenating old samples. Restoring state preserves the original sample time; an expired value stays explicitly stale. Only the latest readings are stored in SQLite; there is no history service, passive wake, automatic memory capture or unsolicited AI action.

Refresh requests coalesce per endpoint, enforce a five-second minimum interval, and wait at most the configured 1–10 seconds (default 10). Background refresh is 30–3600 seconds (default 60). The `data.refresh` object exposes `idle`, `waiting`, `received`, `timeout` or `unavailable`, the configured wait limit and an explanatory message. Timeout returns the existing age/validity, never a fabricated successful reading. The assistant reports the timeout once; any retained values are identified as previous samples with their source time, not as a successful refresh. Do not repeatedly retry within the same answer. The receiver stays running and polling continues after timeout. A newer authenticated report, including a late arrival after timeout, automatically clears the timeout state; each metric still has its own validity/freshness checks. No restart, re-pairing or reconfiguration is needed when the hub starts working again. The optional source retries bridge/listener failures and reports degraded health without stopping voice, tasks or another endpoint. HomeKit keys/IDs survive service restarts; adding another endpoint does not reassign existing accessories.

## Query and acceptance

```sh
personal-agent --config config/config.toml devices list
personal-agent --config config/config.toml devices inspect homepod_environment
.venv/bin/python scripts/verify_environment.py --config config/config.toml --device homepod_environment --codex
```

Device MCP needs no HomePod-specific tool: `list_devices` discovers `sensor.temperature` / `sensor.humidity`; `inspect_device` requests refresh and returns typed readings. The model must inspect each metric's validity/freshness before stating a current value. The acceptance entry uses an isolated provider thread with the installed device socket; it does not play, capture media, change output, save memory or disturb the shared conversation. Evidence is private under `state/acceptance/environment/`. `--restart` additionally checks installed-service recovery without replaying a user task. Synthetic acceptance can check the HTTP → directory → real stdio MCP → Codex path with `--synthetic`; that is expressly not real HomePod success.

Primary references: [Apple HomePod sensor documentation](https://support.apple.com/en-gb/guide/homepod/apd8085c1032/homepod), [upstream Home automation readback pattern](https://github.com/gregbny/homepod-temp-logger/blob/main/TUTORIAL.md), [HAP-python](https://github.com/ikalchev/HAP-python).
