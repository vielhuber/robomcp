"""robomcp — Roborock MCP server.

Thin wrapper around python-roborock (https://github.com/Python-roborock/python-roborock)
that exposes device discovery, status, control and a handful of diagnostic
traits as MCP tools.

Authentication is performed once via the CLI (`robomcp auth`); the resulting
e-mail address, user_data and cached home_data are persisted to
~/.config/robomcp/state.json. The MCP server (`robomcp`) then reads the
state file at every tool call.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
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
    # If `base` is a symlink (set up by the deployment script), make sure the
    # symlink target itself exists — otherwise mkdir(exist_ok=True) on the
    # broken link raises FileExistsError.
    if base.is_symlink():
        target = Path(os.readlink(str(base)))
        if not target.is_absolute():
            target = base.parent / target
        target.mkdir(parents=True, exist_ok=True)
        return base
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
    email = (_load_state().get("email") or "").strip()
    if not email:
        raise RuntimeError("Not authenticated. Run `robomcp auth` first.")
    return email


@asynccontextmanager
async def _open_manager():
    """Create a DeviceManager from persisted user_data, yield it, then close."""
    state = _load_state()
    ud_raw = state.get("user_data")
    if not ud_raw:
        raise RuntimeError("Not authenticated. Run `robomcp auth` first.")
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
async def list_devices() -> list[dict[str, Any]]:
    """List devices from the cached home data (no MQTT roundtrip)."""
    state = _load_state()
    home = state.get("home_data")
    if not home:
        raise RuntimeError("Not authenticated. Run `robomcp auth` first.")
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


# ---------------------------------------------------------------------------
# CLI: interactive authentication
# ---------------------------------------------------------------------------


def _prompt(label: str) -> str:
    """Read a line from stdin, stripped, non-empty."""
    value = input(f"{label}: ").strip()
    if not value:
        print(f"{label} is required.", file=sys.stderr)
        sys.exit(1)
    return value


async def _auth_flow(force: bool = False) -> None:
    """Two-step e-mail verification. Persists email + user_data + home_data."""
    state = _load_state()
    if not force and state.get("user_data") and state.get("email"):
        devices = (state.get("home_data") or {}).get("devices") or []
        print(f"Already authenticated as {state['email']} ({len(devices)} device(s) cached).")
        print(f"State file: {_state_file()}")
        print("Pass --force to re-authenticate.")
        return
    email = _prompt("Roborock e-mail")
    api = RoborockApiClient(email)
    print(f"Sending verification code to {email} ...")
    await api.request_code()
    print("Code sent. Check your inbox.")

    # The same RoborockApiClient instance is used for both request_code and
    # code_login so the per-instance _device_identifier (which is part of the
    # header_clientid the server binds the pending code to) stays consistent.
    code = _prompt("Verification code")
    user_data = await api.code_login(code)
    home = await api.get_home_data_v2(user_data)

    _save_state(
        {
            "email": email,
            "user_data": user_data.as_dict(),
            "home_data": home.as_dict(),
        }
    )
    print(f"Authenticated. {len(home.devices)} device(s) cached:")
    for d in home.devices:
        print(f"  - {d.name} ({d.duid})")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        force = "--force" in sys.argv[2:] or "-f" in sys.argv[2:]
        try:
            asyncio.run(_auth_flow(force=force))
        except KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(130)
        except Exception as e:
            print(f"Authentication failed: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help", "help"):
        print(
            "Usage:\n"
            "  robomcp                  start the MCP server (stdio)\n"
            "  robomcp auth             show cached login or run interactive login if none\n"
            "  robomcp auth --force     force re-authentication\n"
        )
        return
    mcp.run()


if __name__ == "__main__":
    main()
