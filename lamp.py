"""Murray Lamp — Moonside Halo via Nordic UART (NUS) over BLE.

Separate BleakClient from the iDotMatrix panel driver. Discover by name
prefix MOONSIDE (macOS CoreBluetooth UUIDs change; never hardcode address).
Optional env MOONSIDE_MAC pins address/UUID when set.

THEME.* commands must follow https://developer.moonside.design/ exactly (name and
number of RGB triplets, trailing comma): a malformed theme hangs the firmware until
power-cycled. That is what earlier "THEME.* unreliable" notes were seeing.
Brightness command is BRIGH080 (NOT BRIGHTNESS080): firmware parses substring(5).toInt(),
so BRIGHTNESS100 becomes brightness 0 and the lamp stays dark until reboot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("murray-lamp")

NUS_TX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NAME_PREFIX = "MOONSIDE"
DEFAULT_BRIGHTNESS = 100

# Base RGB per presence state, kept for tests/reference; DEFAULT_CONFIG is what
# the lamp actually uses (blink/wink are face overlays — no lamp change).
STATE_RGB: dict[str, tuple[int, int, int]] = {
    "idle": (40, 220, 80),        # solid green, no animation (Jose's pick)
    "thinking": (0, 220, 255),    # cyan
    "happy": (40, 220, 80),       # green
    "speaking": (255, 240, 220),  # warm white
    "error": (255, 40, 40),       # red
    "notify": (200, 0, 255),      # purple (historical input)
}

# Face-only overlays; lamp keeps the parent state color.
LAMP_NOOP_STATES = frozenset({"blink", "wink"})

# Official theme catalog (https://developer.moonside.design/): number of RGB
# triplets each theme takes. -1 = single speed value, 0 = fixed "0" payload.
# Wrong count or unknown name hangs the firmware until power-cycled.
THEME_CATALOG: dict[str, int] = {
    "RAINBOW1": -1, "RAINBOW2": -1, "RAINBOW3": 0, "FIRE1": 0, "PALETTE1": 0,
    "THEME1": 2, "THEME2": 2, "THEME4": 2, "THEME5": 2,
    "COLORDROP1": 2, "GRADIENT1": 2, "GRADIENT3": 2, "TWINKLE1": 2,
    "PULSING1": 2, "BEAT2": 2, "WAVE1": 2,
    "GRADIENT2": 3, "BEAT1": 3, "BEAT3": 3, "LAVA1": 3,
    "FIRE2": 4, "THEME3": 6, "PALETTE2": 6,
}
DEFAULT_THEME_SPEED = 20

_CYAN, _NAVY, _WHITE, _BLACK = [0, 220, 255], [0, 0, 140], [255, 255, 255], [0, 0, 0]
_GREEN, _PURPLE, _RED, _WARM = [40, 220, 80], [200, 0, 255], [255, 40, 40], [255, 240, 220]

# Effect = {"theme": <catalog name> | None (solid), "colors": [[r,g,b], ...]}.
DEFAULT_CONFIG: dict = {
    "brightness": DEFAULT_BRIGHTNESS,
    "manual": None,  # effect dict to hold regardless of state, or None
    "states": {
        "idle": {"theme": None, "colors": [_GREEN]},
        "thinking": {"theme": "BEAT1", "colors": [_CYAN, _NAVY, _WHITE]},
        "happy": {"theme": "TWINKLE1", "colors": [_WHITE, _GREEN]},
        "speaking": {"theme": None, "colors": [_WARM]},
        "error": {"theme": "BEAT3", "colors": [_RED, _BLACK, _WHITE]},
        "notify": {"theme": "WAVE1", "colors": [_PURPLE, _WHITE]},
    },
}

CONFIG_PATH = Path.home() / ".murray-lamp/config.json"
PREVIEW_PATH = Path.home() / ".murray-lamp/preview.json"
PREVIEW_SECONDS = 8.0


def build_color_cmd(r: int, g: int, b: int) -> str:
    return f"COLOR{r:03d}{g:03d}{b:03d}"


def build_brightness_cmd(n: int) -> str:
    n = max(0, min(120, int(n)))
    return f"BRIGH{n:03d}"


def build_theme_cmd(name: str, colors, *, speed: int = DEFAULT_THEME_SPEED) -> str:
    name = name.strip().upper()
    if name not in THEME_CATALOG:
        raise ValueError(f"Unknown Moonside theme: {name!r}")
    need = THEME_CATALOG[name]
    if need == -1:
        payload = str(max(0, min(100, int(speed))))
    elif need == 0:
        payload = "0"
    else:
        if len(colors) < need:
            raise ValueError(f"{name} needs {need} colours, got {len(colors)}")
        payload = ",".join(f"{int(c)}" for rgb in list(colors)[:need] for c in rgb)
    return f"THEME.{name}.{payload},"


def color_for_state(state: str) -> Optional[tuple[int, int, int]]:
    """RGB for a presence state, or None if the lamp should not change."""
    if state in LAMP_NOOP_STATES:
        return None
    return STATE_RGB.get(state)


def effect_commands(effect: dict, *, brightness: Optional[int] = DEFAULT_BRIGHTNESS) -> list[str]:
    """NUS commands for one effect dict (solid colour or catalog theme)."""
    theme = effect.get("theme")
    colors = effect.get("colors") or []
    if theme:
        final = build_theme_cmd(theme, colors, speed=effect.get("speed", DEFAULT_THEME_SPEED))
    else:
        rgb = colors[0] if colors else (0, 0, 0)
        final = build_color_cmd(*(int(c) for c in rgb))
    # A brief LEDOFF before a theme stops the previous colour/effect bleeding through.
    cmds = ["LEDOFF", "LEDON"] if theme else ["LEDON"]
    # BRIGH re-applies the saved mode, so it goes before the theme to avoid a restart.
    if brightness is not None:
        cmds.append(build_brightness_cmd(brightness))
    cmds.append(final)
    return cmds


def _valid_effect(effect) -> bool:
    if not isinstance(effect, dict):
        return False
    try:
        effect_commands(effect)
        return True
    except (ValueError, TypeError):
        return False


def load_config(path: Path = CONFIG_PATH) -> dict:
    """User config merged over DEFAULT_CONFIG; tolerant of missing/corrupt files."""
    import copy
    import json

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    if isinstance(raw.get("brightness"), (int, float)):
        cfg["brightness"] = max(0, min(120, int(raw["brightness"])))
    if _valid_effect(raw.get("manual")):
        cfg["manual"] = raw["manual"]
    for state, effect in (raw.get("states") or {}).items():
        if state in cfg["states"] and _valid_effect(effect):
            cfg["states"][state] = effect
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    import json

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(path)


def commands_for_state(
    state: str,
    *,
    brightness: Optional[int] = None,
    config: Optional[dict] = None,
) -> list[str]:
    """UTF-8 NUS commands to apply for a state (empty if noop/unknown)."""
    if color_for_state(state) is None:
        return []
    cfg = config or DEFAULT_CONFIG
    effect = cfg.get("manual") or cfg["states"][state]
    if brightness is None:
        brightness = cfg.get("brightness", DEFAULT_BRIGHTNESS)
    return effect_commands(effect, brightness=brightness)


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


async def send_commands(client, cmds: list[str]) -> None:
    for cmd in cmds:
        await _send(client, cmd)
        if cmd in ("LEDOFF", "LEDON"):
            await asyncio.sleep(0.3)


async def apply_state(client, state: str, *, brightness: Optional[int] = DEFAULT_BRIGHTNESS) -> None:
    await send_commands(client, commands_for_state(state, brightness=brightness))


class LampPlanner:
    """Decides what to send: config file (hot reload), preview.json, manual mode.

    Pure logic, no BLE — `plan()` returns the command list when the lamp must
    change, else None. The panel writes config/preview; this picks them up.
    """

    def __init__(self, config_path: Path = CONFIG_PATH, preview_path: Path = PREVIEW_PATH):
        self.config_path = Path(config_path)
        self.preview_path = Path(preview_path)
        self._cfg_mtime: Optional[float] = -1.0
        self.config = DEFAULT_CONFIG
        self._preview_seen: Optional[float] = None
        self._preview_until = -1.0
        self._last: Optional[tuple[str, ...]] = None

    def reset(self) -> None:
        self._last = None  # force re-apply (e.g. after reconnect)

    def _refresh_config(self) -> None:
        try:
            mtime = os.stat(self.config_path).st_mtime
        except OSError:
            mtime = None
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            self.config = load_config(self.config_path)
            log.info("Lamp config loaded from %s", self.config_path)

    def _new_preview(self) -> Optional[dict]:
        import json

        try:
            raw = json.loads(self.preview_path.read_text())
            ts = float(raw["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if ts == self._preview_seen:
            return None
        self._preview_seen = ts
        if time.time() - ts > PREVIEW_SECONDS:
            return None
        effect = raw.get("effect")
        return effect if _valid_effect(effect) else None

    def plan(self, state: str, now: float) -> Optional[list[str]]:
        self._refresh_config()
        preview = self._new_preview()
        if preview is not None:
            self._preview_until = now + PREVIEW_SECONDS
            cmds = effect_commands(preview, brightness=self.config["brightness"])
            self._last = tuple(cmds)
            return cmds
        if now < self._preview_until:
            return None
        cmds = commands_for_state(state, config=self.config)
        if not cmds or tuple(cmds) == self._last:
            return None
        self._last = tuple(cmds)
        return cmds


async def drive_lamp(
    machine,
    *,
    dry_run: bool = False,
    poll_interval: float = 0.5,
    planner: Optional[LampPlanner] = None,
):
    """asyncio task: watch StateMachine and drive Moonside on transitions.

    Own BleakClient — never share with drive_panel. Backoff 5s→60s on failure;
    never raises out (lamp off/out of range is soft).
    """
    backoff = 5.0
    planner = planner or LampPlanner()

    if dry_run:
        log.info("Lamp dry-run: logging commands, no BLE")
        while True:
            try:
                cmds = planner.plan(machine.current(time.monotonic()), time.monotonic())
                if cmds:
                    log.info("lamp dry-run cmds=%s", cmds)
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
                planner.reset()
                while True:
                    if not client.is_connected:
                        raise RuntimeError("Lamp disconnected")
                    now = time.monotonic()
                    cmds = planner.plan(machine.current(now), now)
                    if cmds:
                        await send_commands(client, cmds)
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
