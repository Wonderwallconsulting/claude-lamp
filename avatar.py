"""Small, sequential BLE avatar controller for iDotMatrix 32x32 panels."""

import argparse
import asyncio
from contextlib import asynccontextmanager
import math
import logging
from pathlib import Path
import random

from bleak import BleakClient, BleakScanner
from pyidotmatrix import BleDisplay, BleTransport, ScreenSize
from pyidotmatrix.transport.const import UUID_WRITE, UUID_NOTIFY
from expressions import EXPRESSIONS, render


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a finite positive number")
    return number


async def scan(timeout=10):
    try:
        found = await asyncio.wait_for(
            BleakScanner.discover(timeout=timeout, return_adv=True), timeout + 15)
    except TimeoutError as exc:
        raise RuntimeError("BLE initialization timed out. Allow Bluetooth for the host app in macOS "
                           "Privacy & Security, then retry from Terminal.") from exc
    return [(device, adv) for device, adv in found.values()
            if (adv.local_name or device.name or "").upper().startswith("IDM")]


def select_device(found, address=None):
    matches = [d for d, _ in found if address is None or d.address.lower() == address.lower()]
    if len(matches) != 1:
        raise RuntimeError("No unique IDM screen found. Power it on, close the phone app, "
                           "run scan and use --device with its identifier if there are several.")
    return matches[0]


async def validate_gatt(device):
    async with BleakClient(device, timeout=20) as client:
        write = client.services.get_characteristic(UUID_WRITE)
        notify = client.services.get_characteristic(UUID_NOTIFY)
        if write is None or "write" not in write.properties or notify is None or "notify" not in notify.properties:
            raise RuntimeError("The device does not expose the expected writable FA02 / notify FA03 profile")
        print("GATT profile matches iDotMatrix (FA02 write / FA03 notify).")


@asynccontextmanager
async def connected(address=None, timeout=10):
    device = select_device(await scan(timeout), address)
    print(f"Connecting to {device.name or 'IDM'} [{device.address}]", flush=True)
    await asyncio.wait_for(validate_gatt(device), 30)
    transport = BleTransport(mac_address=device.address, auto_reconnect=False)
    display = BleDisplay(ScreenSize.SIZE_32x32, transport)
    failed = False
    try:
        await asyncio.wait_for(display.connect(), 30)
        yield display
    except BaseException:
        failed = True
        raise
    finally:
        try:
            await asyncio.wait_for(display.disconnect(), 10)
        except Exception:
            if not failed:
                raise
            logging.getLogger(__name__).warning("BLE cleanup failed after an earlier error")


async def show(display, expression, phase=0):
    await asyncio.wait_for(display.show_frame(render(expression, phase).tobytes()), 15)


async def animate(display, expression, duration, fps=3):
    deadline = asyncio.get_running_loop().time() + duration
    phase = 0
    while asyncio.get_running_loop().time() < deadline:
        await show(display, expression, phase)
        phase += 1
        await asyncio.sleep(1 / fps)


async def idle(display, seconds=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds if seconds is not None else math.inf
    while loop.time() < deadline:
        await show(display, "idle")
        await asyncio.sleep(min(random.uniform(2.5, 5), max(0, deadline - loop.time())))
        if loop.time() >= deadline:
            break
        await show(display, "blink")
        await asyncio.sleep(0.2)
    await show(display, "idle")


def preview(folder):
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    for name in EXPRESSIONS:
        render(name).resize((320, 320), resample=0).save(folder / f"{name}.png")
        frames.extend(render(name, i).resize((320, 320), resample=0) for i in range(4))
    frames[0].save(folder / "avatar.gif", save_all=True, append_images=frames[1:], duration=300, loop=0)
    print(folder.resolve())


async def run(args):
    if args.command == "preview":
        preview(Path(args.output))
        return
    if args.command == "scan":
        found = await scan(args.timeout)
        for device, adv in found:
            print(f"{adv.local_name or device.name}\t{device.address}\tRSSI {adv.rssi}")
        if not found:
            raise RuntimeError("No IDM advertisements found. Check power, Bluetooth permission and close the phone app.")
        return
    async with connected(args.device, args.timeout) as display:
        if args.command == "demo":
            for expression in ("idle", "blink", "thinking", "happy", "wink", "error"):
                print(expression, flush=True)
                await animate(display, expression, 1.5)
            await idle(display, 8)
            print("Frames sent successfully; confirm the result on the physical screen.")
        elif args.command == "idle":
            await idle(display, args.seconds)
        else:
            await animate(display, args.expression, args.seconds or 3)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("scan", "demo", "idle", "show", "preview"))
    p.add_argument("--device", help="Identifier printed by scan (UUID on macOS)")
    p.add_argument("--timeout", type=positive, default=10)
    p.add_argument("--seconds", type=positive)
    p.add_argument("--expression", choices=EXPRESSIONS, default="happy")
    p.add_argument("--output", default="preview")
    return p


if __name__ == "__main__":
    try:
        asyncio.run(run(parser().parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")
    except Exception as exc:
        raise SystemExit(f"Error: {type(exc).__name__}: {exc}")
