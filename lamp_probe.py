"""Send each official THEME to the Moonside Halo for a few seconds.

Parameter counts follow https://developer.moonside.design/ exactly — a
malformed THEME (wrong color count, unknown name) can hang the firmware.
Run via launchd through Murray Lamp.app (TCC) with the daemon stopped.
"""

import asyncio
import logging
import sys

from bleak import BleakClient

from lamp import _send, discover_moonside

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

HOLD = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0

CYAN, BLUE, PURPLE, WHITE, RED, MANGO, GREEN = (
    "0,220,255", "0,0,140", "200,0,255", "255,255,255", "255,40,40", "255,180,50", "40,220,80")

THEMES = [
    f"THEME.RAINBOW1.20,",
    f"THEME.RAINBOW3.0,",
    f"THEME.FIRE1.0,",
    f"THEME.FIRE2.0,0,0,255,0,0,255,120,0,255,255,255,",
    f"THEME.THEME1.{CYAN},{BLUE},",
    f"THEME.THEME2.{CYAN},{BLUE},",
    f"THEME.THEME4.{CYAN},{BLUE},",
    f"THEME.THEME5.{PURPLE},{BLUE},",
    f"THEME.COLORDROP1.{CYAN},{BLUE},",
    f"THEME.GRADIENT1.{CYAN},{BLUE},",
    f"THEME.GRADIENT2.{CYAN},{PURPLE},{WHITE},",
    f"THEME.GRADIENT3.{CYAN},{BLUE},",
    f"THEME.TWINKLE1.{WHITE},{BLUE},",
    f"THEME.PULSING1.{CYAN},{BLUE},",
    f"THEME.BEAT1.{CYAN},{BLUE},{WHITE},",
    f"THEME.BEAT2.{WHITE},{BLUE},",
    f"THEME.BEAT3.{RED},{BLUE},{WHITE},",
    f"THEME.WAVE1.{PURPLE},{WHITE},",
    f"THEME.LAVA1.{RED},{MANGO},{WHITE},",
    f"THEME.THEME3.{CYAN},{BLUE},{PURPLE},{WHITE},{MANGO},{GREEN},",
]


async def main():
    dev = await discover_moonside()
    if dev is None:
        sys.exit(1)
    async with BleakClient(dev) as client:
        await _send(client, "LEDOFF")
        await asyncio.sleep(0.5)
        await _send(client, "LEDON")
        await asyncio.sleep(0.5)
        await _send(client, "BRIGH100")
        await asyncio.sleep(0.5)
        for i, cmd in enumerate(THEMES, 1):
            print(f"\n>>> #{i} {cmd}", flush=True)
            await _send(client, cmd)
            await asyncio.sleep(HOLD)
            await _send(client, f"COLOR255180050")
            await asyncio.sleep(3.0)
        await _send(client, "COLOR255180050")


asyncio.run(main())
