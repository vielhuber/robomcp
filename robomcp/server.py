"""robomcp — Roborock MCP server.

Thin wrapper around python-roborock (https://github.com/Python-roborock/python-roborock)
that exposes login, device discovery, status, control and a handful of
diagnostic traits as MCP tools.

State (device identifier, user data, cached home data) is persisted to a
JSON file under ~/.config/robomcp/state.json so the two-step e-mail
verification flow survives between separate tool invocations.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from roborock.data.containers import UserData
from roborock.devices.device_manager import (
    UserParams,
    create_device_manager,
)
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient


# Pretty fan-speed and water-mode names that the device accepts.
FanSpeed = Literal["quiet", "balanced", "turbo", "max", "gentle"]
WaterMode = Literal["off", "low", "medium", "high"]
FAN_SPEED_CODES: dict[str, int] = {
    "quiet": 101, "balanced": 102, "turbo": 103, "max": 104, "gentle": 105,
}
WATER_MODE_CODES: dict[str, int] = {
    "off": 200, "low": 201, "medium": 202, "high": 203,
}


def _data_dir() -> Path:
    base = Path.home() / ".config" / "robomcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _state_file() -> Path:
    return _data_dir() / "state.json"


def _load_state() -> dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_state(state: dict[str, Any]) -> None:
    path = _state_file()
    path.write_text(json.dumps(state, default=str, indent=2))
    # State contains rriot tokens — restrict to owner.
    path.chmod(0o600)


def _email() -> str:
    email = os.environ.get("ROBOROCK_EMAIL", "").strip()
    if not email:
        raise RuntimeError("ROBOROCK_EMAIL env var is required")
    return email


@asynccontextmanager
async def _open_manager():
    """Create a DeviceManager from persisted user_data, yield it, then close."""
    state = _load_state()
    ud_raw = state.get("user_data")
    if not ud_raw:
        raise RuntimeError("Not logged in. Call request_code then login first.")
    user_data = UserData.from_dict(ud_raw)
    manager = await create_device_manager(
        UserParams(username=_email(), user_data=user_data)
    )
    try:
        yield manager
    finally:
        await manager.close()


async def _pick_device(manager, duid: str | None):
    devices = await manager.get_devices()
    if not devices:
        raise RuntimeError("No devices on this account")
    if duid is None:
        return devices[0]
    for dev in devices:
        if dev.duid == duid:
            return dev
    raise RuntimeError(f"Device with duid {duid} not found")


def _v1(dev) -> Any:
    props = dev.v1_properties
    if props is None:
        raise RuntimeError(f"Device {dev.name} has no v1 properties")
    return props


# Reused argument annotations.
DuidArg = Annotated[
    str | None,
    Field(
        description="Device UID. Omit to act on the first device on the account."
    ),
]


mcp = FastMCP("robomcp")


@mcp.tool()
async def request_code() -> str:
    """Send a 6-digit verification code to the Roborock account e-mail.

    First step of the two-step login. Reads the e-mail address from
    `ROBOROCK_EMAIL` and persists the per-session device identifier so
    `login` can reproduce the same identity.
    """
    email = _email()
    api = RoborockApiClient(email)
    # Persist the per-instance device identifier so login() can reconstruct
    # the same header_clientid (MD5(email + identifier)) the server binds
    # the pending code to.
    state = _load_state()
    state["device_identifier"] = api._device_identifier
    _save_state(state)
    await api.request_code()
    return f"Code sent to {email}. Now call login(code)."


@mcp.tool()
async def login(
    code: Annotated[
        str,
        Field(
            description="6-digit code received by e-mail. Pass as STRING so "
            "leading zeros are preserved (e.g. '058537').",
            min_length=4,
            max_length=8,
        ),
    ],
) -> str:
    """Confirm the e-mail code and cache user_data + device list.

    Second step of the two-step login. After this call, all other tools
    work without further authentication.
    """
    email = _email()
    state = _load_state()
    identifier = state.get("device_identifier")
    if not identifier:
        raise RuntimeError("No pending code. Call request_code first.")
    api = RoborockApiClient(email)
    api._device_identifier = identifier
    user_data = await api.code_login(str(code))
    home = await api.get_home_data_v2(user_data)
    state["user_data"] = user_data.as_dict()
    state["home_data"] = home.as_dict()
    _save_state(state)
    return (
        f"Logged in. {len(home.devices)} device(s) cached: "
        + ", ".join(f"{d.name} ({d.duid})" for d in home.devices)
    )


@mcp.tool()
async def list_devices() -> list[dict[str, Any]]:
    """List devices from the cached home data (no MQTT roundtrip)."""
    state = _load_state()
    home = state.get("home_data")
    if not home:
        raise RuntimeError("Not logged in. Call request_code then login first.")
    products = {p["id"]: p for p in home.get("products", [])}
    out: list[dict[str, Any]] = []
    for d in home.get("devices", []):
        product = products.get(d.get("productId"), {})
        out.append(
            {
                "duid": d.get("duid"),
                "name": d.get("name"),
                "online": d.get("online"),
                "product_model": product.get("model"),
                "product_name": product.get("name"),
            }
        )
    return out


@mcp.tool()
async def get_status(duid: DuidArg = None) -> dict[str, Any]:
    """Current status of a vacuum.

    Refreshes via MQTT and returns the most useful fields: state, battery,
    error, fan/water modes, last clean area/time, dock state and the
    cleaning/returning flags.
    """
    async with _open_manager() as manager:
        dev = await _pick_device(manager, duid)
        p = _v1(dev)
        await p.status.refresh()
        s = p.status
        return {
            "duid": dev.duid,
            "name": dev.name,
            "online": dev.is_connected,
            "state": s.state_name,
            "battery": s.battery,
            "error": s.error_code_name,
            "fan_speed": s.fan_speed_name,
            "water_mode": s.water_mode_name,
            "last_clean_area_m2": s.square_meter_clean_area,
            "last_clean_time_s": s.clean_time,
            "dock_state": s.dock_state,
            "map_present": bool(s.map_present),
            "in_cleaning": bool(s.in_cleaning),
            "in_returning": bool(s.in_returning),
            "charge_status": s.charge_status,
        }


@mcp.tool()
async def get_rooms(duid: DuidArg = None) -> list[dict[str, Any]]:
    """Rooms / segments known to the vacuum from the current map.

    Returns each room's `segment_id` (use with `clean_rooms`) and its name.
    """
    async with _open_manager() as manager:
        dev = await _pick_device(manager, duid)
        p = _v1(dev)
        await p.rooms.refresh()
        return [
            {
                "segment_id": r["segmentId"],
                "iot_id": r["iotId"],
                "name": r["rawName"],
            }
            for r in p.rooms.as_dict().get("rooms", [])
        ]


@mcp.tool()
async def get_consumables(duid: DuidArg = None) -> dict[str, int]:
    """Consumable run-times in seconds (main brush, side brush, filter, sensors, dust bag).

    Roborock recommends replacement at ~300h main brush, ~200h side brush,
    ~150h filter, ~30h sensor cleaning.
    """
    async with _open_manager() as manager:
        dev = await _pick_device(manager, duid)
        p = _v1(dev)
        await p.consumables.refresh()
        c = p.consumables.as_dict()
        return {
            "main_brush_s": c["mainBrushWorkTime"],
            "side_brush_s": c["sideBrushWorkTime"],
            "filter_s": c["filterWorkTime"],
            "filter_element_s": c["filterElementWorkTime"],
            "sensor_dirty_s": c["sensorDirtyTime"],
            "dust_collection_count": c["dustCollectionWorkTimes"],
        }


@mcp.tool()
async def get_clean_summary(duid: DuidArg = None) -> dict[str, int]:
    """Lifetime cleaning statistics (total time, total area, run count)."""
    async with _open_manager() as manager:
        dev = await _pick_device(manager, duid)
        p = _v1(dev)
        await p.clean_summary.refresh()
        s = p.clean_summary.as_dict()
        return {
            "total_clean_time_s": s["cleanTime"],
            "total_clean_area_mm2": s["cleanArea"],
            "clean_count": s["cleanCount"],
            "dust_collection_count": s["dustCollectionCount"],
        }


@mcp.tool()
async def get_network_info(duid: DuidArg = None) -> dict[str, Any]:
    """Network info reported by the vacuum (IP, SSID, MAC, BSSID, RSSI)."""
    async with _open_manager() as manager:
        dev = await _pick_device(manager, duid)
        p = _v1(dev)
        await p.network_info.refresh()
        n = p.network_info.as_dict()
        return {
            "ip": n.get("ip"),
            "ssid": n.get("ssid"),
            "mac": n.get("mac"),
            "bssid": n.get("bssid"),
            "rssi": n.get("rssi"),
        }


async def _send_command(
    duid: str | None, command: RoborockCommand, params: Any = None
) -> str:
    async with _open_manager() as manager:
        dev = await _pick_device(manager, duid)
        p = _v1(dev)
        await p.command.send(command, params=params)
        suffix = f" params={params}" if params is not None else ""
        return f"Sent {command.value}{suffix} to {dev.name} ({dev.duid})"


@mcp.tool()
async def start(duid: DuidArg = None) -> str:
    """Start (or resume) a full cleaning run."""
    return await _send_command(duid, RoborockCommand.APP_START)


@mcp.tool()
async def pause(duid: DuidArg = None) -> str:
    """Pause the current cleaning. Resume with `start`."""
    return await _send_command(duid, RoborockCommand.APP_PAUSE)


@mcp.tool()
async def stop(duid: DuidArg = None) -> str:
    """Stop cleaning without docking (clears the active job)."""
    return await _send_command(duid, RoborockCommand.APP_STOP)


@mcp.tool()
async def dock(duid: DuidArg = None) -> str:
    """Return to the charging dock."""
    return await _send_command(duid, RoborockCommand.APP_CHARGE)


@mcp.tool()
async def locate(duid: DuidArg = None) -> str:
    """Make the vacuum beep so you can find it."""
    return await _send_command(duid, RoborockCommand.FIND_ME)


@mcp.tool()
async def clean_rooms(
    room_ids: Annotated[
        list[int],
        Field(
            description="List of segment IDs to clean, as returned by "
            "`get_rooms` (the `segment_id` field).",
            min_length=1,
        ),
    ],
    duid: DuidArg = None,
) -> str:
    """Clean a specific set of rooms / segments on the current map."""
    return await _send_command(duid, RoborockCommand.APP_SEGMENT_CLEAN, room_ids)


@mcp.tool()
async def set_fan_speed(
    level: Annotated[FanSpeed, Field(description="Suction power.")],
    duid: DuidArg = None,
) -> str:
    """Set vacuum suction power."""
    return await _send_command(
        duid, RoborockCommand.SET_CUSTOM_MODE, [FAN_SPEED_CODES[level]]
    )


@mcp.tool()
async def set_water_mode(
    level: Annotated[WaterMode, Field(description="Mop water flow.")],
    duid: DuidArg = None,
) -> str:
    """Set the mop water flow level."""
    return await _send_command(
        duid, RoborockCommand.SET_WATER_BOX_CUSTOM_MODE, [WATER_MODE_CODES[level]]
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
