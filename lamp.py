"""Murray Lamp — Moonside Halo via Nordic UART (NUS) over BLE.

Separate BleakClient from the iDotMatrix panel driver. Discover by name
prefix MOONSIDE (macOS CoreBluetooth UUIDs change; never hardcode address).
Optional env MOONSIDE_MAC pins address/UUID when set.

Solid COLOR commands only — THEME.* are unreliable on this Halo.
Brightness command is BRIGH080 (NOT BRIGHTNESS080): firmware parses substring(5).toInt(),
so BRIGHTNESS100 becomes brightness 0 and the lamp stays dark until reboot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

log = logging.getLogger("murray-lamp")

NUS_TX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NAME_PREFIX = "MOONSIDE"
DEFAULT_BRIGHTNESS = 100

# Solid RGB per presence state (blink/wink are face overlays — no lamp change).
STATE_RGB: dict[str, tuple[int, int, int]] = {
    "idle": (255, 180, 50),       # sunset mango (historical)
    "thinking": (0, 220, 255),    # cyan
    "happy": (40, 220, 80),       # green
    "speaking": (255, 240, 220),  # warm white
    "error": (255, 40, 40),       # red
    "notify": (200, 0, 255),      # purple (historical input)
}

# Face-only overlays; lamp keeps the parent state color.
LAMP_NOOP_STATES = frozenset({"blink", "wink"})


def build_color_cmd(r: int, g: int, b: int) -> str:
    return f"COLOR{r:03d}{g:03d}{b:03d}"


def build_brightness_cmd(n: int) -> str:
    n = max(0, min(120, int(n)))
    return f"BRIGH{n:03d}"


def color_for_state(state: str) -> Optional[tuple[int, int, int]]:
    """RGB for a presence state, or None if the lamp should not change."""
    if state in LAMP_NOOP_STATES:
        return None
    return STATE_RGB.get(state)


def commands_for_state(
    state: str,
    *,
    brightness: Optional[int] = DEFAULT_BRIGHTNESS,
) -> list[str]:
    """UTF-8 NUS commands to apply for a state (empty if noop/unknown)."""
    rgb = color_for_state(state)
    if rgb is None:
        return []
    cmds = ["LEDON", build_color_cmd(*rgb)]
    if brightness is not None:
        cmds.append(build_brightness_cmd(brightness))
    return cmds


async def discover_moonside(timeout: float = 10.0):
    """Scan for MOONSIDE*; honor MOONSIDE_MAC when set. Returns BLEDevice or None."""
    from bleak import BleakScanner

    mac = os.environ.get("MOONSIDE_MAC")
    log.info("Scanning for %s* (timeout=%.0fs)%s",
             NAME_PREFIX, timeout,
             f" or MAC={mac}" if mac else "")
    devices = await BleakScanner.discover(timeout=timeout)
    by_mac = None
    by_name = None
    for d in devices:
        name = (d.name or "")
        if mac and d.address.upper() == mac.upper():
            by_mac = d
        if name.upper().startswith(NAME_PREFIX):
            by_name = d
    found = by_mac or by_name
    if found:
        log.info("Found lamp: %s (%s)", found.name, found.address)
    else:
        log.warning("No %s* device found", NAME_PREFIX)
    return found


async def _send(client, cmd: str) -> None:
    log.info("TX %s", cmd)
    await client.write_gatt_char(NUS_TX_UUID, cmd.encode("utf-8"), response=True)


async def apply_state(client, state: str, *, brightness: Optional[int] = DEFAULT_BRIGHTNESS) -> None:
    cmds = commands_for_state(state, brightness=brightness)
    for i, cmd in enumerate(cmds):
        await _send(client, cmd)
        if i == 0 and cmd == "LEDON":
            await asyncio.sleep(0.3)


async def drive_lamp(
    machine,
    *,
    dry_run: bool = False,
    poll_interval: float = 0.5,
    brightness: int = DEFAULT_BRIGHTNESS,
):
    """asyncio task: watch StateMachine and drive Moonside on transitions.

    Own BleakClient — never share with drive_panel. Backoff 5s→60s on failure;
    never raises out (lamp off/out of range is soft).
    """
    backoff = 5.0
    last_state: Optional[str] = None

    if dry_run:
        log.info("Lamp dry-run: logging commands, no BLE")
        while True:
            try:
                state = machine.current(time.monotonic())
                if state != last_state:
                    cmds = commands_for_state(state, brightness=brightness)
                    if cmds:
                        log.info("lamp dry-run state=%s cmds=%s", state, cmds)
                    last_state = state
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Lamp dry-run loop error (%s: %s)", type(exc).__name__, exc)
                await asyncio.sleep(poll_interval)
        return

    while True:
        try:
            device = await discover_moonside()
            if device is None:
                raise RuntimeError("Moonside not advertising")

            from bleak import BleakClient

            async with BleakClient(device, timeout=15.0) as client:
                log.info("Lamp connected (%s)", device.name or device.address)
                backoff = 5.0
                last_state = None  # force re-apply after reconnect
                while True:
                    if not client.is_connected:
                        raise RuntimeError("Lamp disconnected")
                    state = machine.current(time.monotonic())
                    if state != last_state:
                        await apply_state(client, state, brightness=brightness)
                        last_state = state
                    await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Lamp BLE unavailable (%s: %s); retrying in %.0fs",
                        type(exc).__name__, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# Demo / priority order for solid colors (matches presence PRIORITY sans idle-last).
DEMO_STATES = ("error", "speaking", "happy", "thinking", "notify", "idle")


async def demo_cycle(
    *,
    dry_run: bool = False,
    hold: float = 2.0,
    brightness: int = 100,
    lamp_hold: float = 3.0,
) -> None:
    """Best-effort cycle of lamp colors for --once-demo. Soft-fail if lamp off.

    Ends on idle mango and never sends LEDOFF. Closing BleakClient may still
    dim/power-off the Halo on GATT disconnect — use drive_lamp (daemon) to keep
    the connection open, or pass lamp_hold to leave idle visible a few seconds.
    """
    states = [s for s in DEMO_STATES if s in STATE_RGB]
    if dry_run:
        for state in states:
            cmds = commands_for_state(state, brightness=brightness)
            log.info("lamp demo dry-run state=%s cmds=%s", state, cmds)
            print(f"lamp:{state} -> {cmds}", flush=True)
            await asyncio.sleep(0.05)
        print(
            "lamp may dim/off when BLE disconnects; run daemon to keep lit",
            flush=True,
        )
        return

    try:
        device = await discover_moonside(timeout=8.0)
        if device is None:
            log.warning("Lamp demo skipped: device not found")
            return
        from bleak import BleakClient

        async with BleakClient(device, timeout=15.0) as client:
            for state in states:
                print(f"lamp:{state}", flush=True)
                await apply_state(client, state, brightness=brightness)
                await asyncio.sleep(hold)
            # Final idle already applied (last DEMO_STATES); hold so it's visible.
            # Never LEDOFF — historical moonside_daemon sent LEDOFF on exit; we don't.
            if lamp_hold > 0:
                log.info("Lamp demo hold idle %.1fs before disconnect", lamp_hold)
                await asyncio.sleep(lamp_hold)
        print(
            "lamp may dim/off when BLE disconnects; run daemon to keep lit",
            flush=True,
        )
    except Exception as exc:
        log.warning("Lamp demo soft-fail (%s: %s)", type(exc).__name__, exc)
