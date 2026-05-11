# 🧼 robomcp 🧼

Minimal MCP server that wraps [python-roborock](https://github.com/Python-roborock/python-roborock) so an MCP-aware client (Claude Code, Claude Desktop, …) can talk to a Roborock vacuum. Auth uses Roborock's two-step e-mail verification: ask for a code, type the code back, done. Credentials and the resulting `user_data` are persisted to `~/.config/robomcp/state.json` (chmod 600) so subsequent calls just work.

## Install

```bash
git clone https://github.com/vielhuber/robomcp.git
cd robomcp
uv tool install .
```

## Configure

```json
{
    "robomcp": {
        "command": "robomcp",
        "env": {
            "ROBOROCK_EMAIL": "you@example.com"
        }
    }
}
```

## Tools

### Authentication (one-time)

#### `request_code()`

Sends a 6-digit verification code to `ROBOROCK_EMAIL`. Persists the per-session device identifier so `login` can reproduce the same identity.

#### `login(code: str)`

Confirms the code received by e-mail. **Pass the code as a string** (`"058537"`) so leading zeros are preserved. Caches `user_data` and the device list — all other tools then work without re-auth.

### Discovery

#### `list_devices()`

Lists devices from the cached home data (no MQTT roundtrip). Returns `duid`, `name`, `online`, `product_model`, `product_name` per device.

#### `get_rooms(duid?)`

Rooms / segments on the current map. Returns each room's `segment_id` (use it with `clean_rooms`) and its name.

### Status & diagnostics

#### `get_status(duid?)`

Live status: `state`, `battery`, `error`, `fan_speed`, `water_mode`, `last_clean_area_m2`, `last_clean_time_s`, `dock_state`, `map_present`, `in_cleaning`, `in_returning`, `charge_status`.

#### `get_consumables(duid?)`

Run-times (seconds) of main brush, side brush, filter, filter element, sensors, and the dust-collection counter. Roborock recommends replacement at roughly: main brush 300 h, side brush 200 h, filter 150 h, sensor cleaning 30 h.

#### `get_clean_summary(duid?)`

Lifetime stats: total clean time (s), total area (mm²), run count, dust-collection count.

#### `get_network_info(duid?)`

IP, SSID, MAC, BSSID, RSSI as reported by the vacuum.

### Control

#### `start(duid?)`

Start (or resume) a full cleaning run.

#### `pause(duid?)`

Pause the current run. Resume with `start`.

#### `stop(duid?)`

Stop cleaning without docking (clears the active job).

#### `dock(duid?)`

Return to the charging dock.

#### `locate(duid?)`

Beep the vacuum so you can find it.

#### `clean_rooms(room_ids: list[int], duid?)`

Clean specific rooms. Pass segment IDs from `get_rooms`.

#### `set_fan_speed(level: str, duid?)`

Set suction. `level` ∈ `quiet | balanced | turbo | max | gentle`.

#### `set_water_mode(level: str, duid?)`

Set mop water flow. `level` ∈ `off | low | medium | high`.

> `duid` is optional everywhere — omit to act on the first device on the account.
