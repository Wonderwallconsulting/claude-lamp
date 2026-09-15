#!/usr/bin/env python3
"""Murray LED Presence daemon.

Turns the iDotMatrix 32x32 panel into a live physical avatar for Murray/Hermes
and mirrors state on a Moonside Halo lamp (Nordic UART BLE). Tails Hermes log
files (read-only) to infer system state. Panel and lamp each own a separate
BleakClient; resilient when either device is off (silent reconnect with backoff).

States (priority): error > speaking > happy > thinking > notify > idle.
"""

import argparse
import asyncio
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("murray-presence")

# ---------------------------------------------------------------------------
# Log line parsing
# ---------------------------------------------------------------------------

AGENT_LOG = Path.home() / ".hermes/logs/agent.log"
GATEWAY_ERROR_LOG = Path.home() / ".hermes/logs/gateway.error.log"

# Patterns matched against real Hermes log lines (see tests for real samples).
RE_TURN = re.compile(r"conversation turn:")
RE_API_CALL = re.compile(r"API call #\d+")
RE_RESPONSE_READY = re.compile(r"response ready:")
RE_ERROR = re.compile(r"(?:\bERROR\b|RateLimitError|^Traceback \(most recent call last\):)")
RE_NOTIFY = re.compile(r"\bdeliver(?:ing|ed)\b", re.IGNORECASE)
RE_NOTIFY_SKIP = re.compile(r"skipping delivery|empty stdout", re.IGNORECASE)


def parse_agent_line(line: str):
    """Map one agent.log line to an event name, or None."""
    if RE_TURN.search(line):
        return "thinking"
    if RE_API_CALL.search(line):
        return "thinking"
    if RE_RESPONSE_READY.search(line):
        return "speaking"
    if RE_NOTIFY.search(line) and not RE_NOTIFY_SKIP.search(line):
        return "notify"
    return None


def parse_gateway_error_line(line: str):
    """Map one gateway.error.log line to an event name, or None."""
    if RE_ERROR.search(line):
        return "error"
    return None


# Appended by claude_hook.sh from Claude Code hooks: "<epoch> <HookEventName>".
CLAUDE_EVENTS_LOG = Path.home() / ".murray-lamp/claude-events.log"

CLAUDE_HOOK_EVENTS = {
    "UserPromptSubmit": "thinking",
    "PreToolUse": "thinking",
    "PostToolUse": "thinking",
    "SubagentStart": "thinking",
    "Stop": "speaking",
    "PostToolUseFailure": "error",
    "Notification": "waiting",
    "PermissionRequest": "waiting",
}


def parse_claude_event_line(line: str):
    """Map one claude-events.log line to an event name, or None."""
    parts = line.split()
    if len(parts) != 2:
        return None
    return CLAUDE_HOOK_EVENTS.get(parts[1])


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

SPEAKING_SECONDS = 4.0
HAPPY_SECONDS = 2.0
ERROR_SECONDS = 6.0
NOTIFY_SECONDS = 3.0
THINKING_TTL = 20.0  # thinking persists while turn/API-call lines keep arriving

PRIORITY = ("error", "speaking", "happy", "thinking", "notify", "idle")


@dataclass
class StateMachine:
    """Timed, priority-ordered avatar state derived from log events."""

    expires: dict = field(default_factory=dict)  # state -> monotonic expiry

    def feed(self, event: str, now: float) -> None:
        if event == "thinking":
            self.expires["thinking"] = now + THINKING_TTL
        elif event == "speaking":
            self.expires["speaking"] = now + SPEAKING_SECONDS
            self.expires["happy"] = now + SPEAKING_SECONDS + HAPPY_SECONDS
            # A finished response ends the thinking window.
            self.expires.pop("thinking", None)
        elif event == "error":
            self.expires["error"] = now + ERROR_SECONDS
        elif event == "notify":
            self.expires["notify"] = now + NOTIFY_SECONDS
        elif event == "waiting":
            # Claude is blocked on the user: notify must win over thinking.
            self.expires["notify"] = now + NOTIFY_SECONDS
            self.expires.pop("thinking", None)
        else:
            raise ValueError(f"Unknown event: {event!r}")

    def current(self, now: float) -> str:
        for state in PRIORITY[:-1]:
            if self.expires.get(state, -math.inf) > now:
                return state
        return "idle"


# ---------------------------------------------------------------------------
# Incremental log tailing with rotation detection (tail -F style)
# ---------------------------------------------------------------------------

class LogTail:
    def __init__(self, path: Path, from_start: bool = False):
        self.path = path
        self.offset = 0
        self.inode = None
        self.primed = from_start  # if True, read existing content too

    def poll(self):
        """Return a list of new complete lines since the last poll."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self.inode = None
            self.offset = 0
            return []
        rotated = self.inode is not None and stat.st_ino != self.inode
        truncated = stat.st_size < self.offset
        if self.inode is None:
            self.inode = stat.st_ino
            self.offset = 0 if self.primed else stat.st_size
            if not self.primed:
                return []
        elif rotated or truncated:
            self.inode = stat.st_ino
            self.offset = 0
        if stat.st_size <= self.offset:
            return []
        with open(self.path, "rb") as fh:
            fh.seek(self.offset)
            chunk = fh.read(stat.st_size - self.offset)
        # Keep any trailing partial line for the next poll.
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self.offset += end + 1
        return chunk[: end + 1].decode("utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------------------
# Observer task: logs -> state machine
# ---------------------------------------------------------------------------

_RE_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _parse_log_ts(line: str):
    """Wall-clock seconds from a Hermes log prefix, or None."""
    m = _RE_LOG_TS.match(line.lstrip())
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


async def observe(machine: StateMachine, transitions, from_start=False,
                  agent_log=AGENT_LOG, gateway_log=GATEWAY_ERROR_LOG,
                  claude_log=CLAUDE_EVENTS_LOG,
                  poll_interval=1.0, stop_after=None):
    """Tail logs, feed the machine, and report state transitions.

    `transitions` is a callable(state, reason_line) invoked on every change.
    With ``from_start``, the initial historical batch is merged across both
    logs and replayed in timestamp order so speaking→happy→idle cascades
    survive dry-run verification.
    """
    tails = [
        (LogTail(agent_log, from_start), parse_agent_line),
        (LogTail(gateway_log, from_start), parse_gateway_error_line),
        (LogTail(claude_log, from_start), parse_claude_event_line),
    ]
    last_state = None
    deadline = time.monotonic() + stop_after if stop_after else math.inf

    def emit(state, reason):
        nonlocal last_state
        if state != last_state:
            transitions(state, reason)
            last_state = state

    if from_start:
        dated = []
        undated = []
        last_wall = None
        for tail, parse in tails:
            for line in tail.poll():
                event = parse(line)
                if not event:
                    continue
                wall = _parse_log_ts(line)
                if wall is None:
                    undated.append((last_wall, event, line))
                else:
                    last_wall = wall
                    dated.append((wall, event, line))
        dated.sort(key=lambda x: x[0])
        # Attach undated lines (e.g. bare Traceback) next to the prior stamp.
        for wall, event, line in undated:
            dated.append((wall if wall is not None else (dated[-1][0] if dated else time.time()),
                          event, line))
        dated.sort(key=lambda x: x[0])
        if dated:
            mono0 = time.monotonic()
            t0 = dated[0][0]
            for wall, event, line in dated:
                now = mono0 + (wall - t0)
                emit(machine.current(now), "(timer expiry)")
                machine.feed(event, now)
                emit(machine.current(now), line.strip())
            # Drain timed states on the synthetic clock, then reset so live
            # tailing uses real monotonic times without stale far-future expires.
            drain = (mono0 + (dated[-1][0] - t0)
                     + max(SPEAKING_SECONDS + HAPPY_SECONDS, ERROR_SECONDS,
                           THINKING_TTL, NOTIFY_SECONDS) + 1.0)
            emit(machine.current(drain), "(timer expiry)")
            machine.expires.clear()

    while time.monotonic() < deadline:
        now = time.monotonic()
        for tail, parse in tails:
            for line in tail.poll():
                event = parse(line)
                if event:
                    machine.feed(event, now)
                    emit(machine.current(now), line.strip())
        emit(machine.current(time.monotonic()), "(timer expiry)")
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# BLE task: state machine -> panel, with backoff reconnect
# ---------------------------------------------------------------------------

async def drive_panel(machine: StateMachine, device: str | None, fps=3.0):
    from avatar import connected, show  # BLE deps imported lazily

    backoff = 5.0
    while True:
        try:
            async with connected(device) as display:
                log.info("Panel connected")
                backoff = 5.0
                phase = 0
                while True:
                    await show(display, machine.current(time.monotonic()), phase)
                    phase += 1
                    await asyncio.sleep(1 / fps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("BLE unavailable (%s: %s); retrying in %.0fs",
                        type(exc).__name__, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def once_demo(device: str | None, *, lamp: bool = True, panel: bool = True,
                   lamp_dry_run: bool = False):
    """Cycle panel expressions and/or lamp solid colors (best-effort)."""
    lamp_task = None
    if lamp:
        from lamp import demo_cycle
        lamp_task = asyncio.create_task(demo_cycle(dry_run=lamp_dry_run, hold=2.0))

    if panel:
        from avatar import connected, animate
        from expressions import EXPRESSIONS

        try:
            async with connected(device) as display:
                await display.set_power(True)
                await display.set_brightness(80)
                for expression in EXPRESSIONS:
                    print(expression, flush=True)
                    await animate(display, expression, 3.0)
                print("idle hold (animated bob)", flush=True)
                await animate(display, "idle", 10.0)
            print("Demo frames sent; confirm on the physical screen.")
        except Exception as exc:
            log.warning("Panel demo soft-fail (%s: %s)", type(exc).__name__, exc)
            if not lamp:
                raise
    elif lamp and lamp_task is not None:
        # Lamp-only: wait for the color cycle to finish.
        await lamp_task
        lamp_task = None
        print("Lamp demo cycle finished (best-effort).")

    if lamp_task is not None:
        try:
            await lamp_task
        except Exception as exc:
            log.warning("Lamp demo soft-fail (%s: %s)", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def run_daemon(args):
    machine = StateMachine()

    def report(state, line):
        log.info("state -> %-8s | %s", state, line[:160])

    tasks = [asyncio.create_task(observe(machine, report), name="observe")]
    if not getattr(args, "lamp_only", False):
        tasks.append(asyncio.create_task(drive_panel(machine, args.device), name="panel"))
    if not getattr(args, "no_lamp", False):
        from lamp import drive_lamp
        tasks.append(asyncio.create_task(
            drive_lamp(machine, dry_run=False), name="lamp"))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_dry(args):
    machine = StateMachine()
    count = [0]

    def report(state, line):
        count[0] += 1
        print(f"[{time.strftime('%H:%M:%S')}] state -> {state:8s} | {line[:150]}", flush=True)

    await observe(machine, report, from_start=args.from_start,
                  poll_interval=0.2, stop_after=args.seconds)
    print(f"-- dry-run finished: {count[0]} transitions detected --")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=os.environ.get("MURRAY_LED_DEVICE"),
                   help="BLE identifier (UUID on macOS) of the IDM panel")
    p.add_argument("--once-demo", action="store_true",
                   help="Cycle panel expressions and lamp colors, then exit")
    p.add_argument("--dry-run", action="store_true",
                   help="No BLE: print state transitions from real logs")
    p.add_argument("--from-start", action="store_true",
                   help="With --dry-run: replay existing log content from the beginning")
    p.add_argument("--seconds", type=float, default=None,
                   help="With --dry-run: stop after N seconds (default: run forever)")
    p.add_argument("--no-lamp", action="store_true",
                   help="Do not drive the Moonside Halo lamp")
    p.add_argument("--lamp-only", action="store_true",
                   help="Drive only the Moonside lamp (skip iDotMatrix panel)")
    args = p.parse_args()
    if args.no_lamp and args.lamp_only:
        p.error("--no-lamp and --lamp-only are mutually exclusive")

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.once_demo:
            asyncio.run(once_demo(
                args.device,
                lamp=not args.no_lamp,
                panel=not args.lamp_only,
                lamp_dry_run=args.dry_run,
            ))
        elif args.dry_run:
            asyncio.run(run_dry(args))
        else:
            asyncio.run(run_daemon(args))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
