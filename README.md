# Murray LED Presence

Live physical avatar for Murray/Hermes on an iDotMatrix 32x32 BLE LED panel
(`IDM-29BA80`, macOS UUID `1D6E439A-F83F-C1AE-B31B-6F09BBC97163`).

A single daemon (`presence_daemon.py`) owns the BLE connection and tails
Hermes logs **read-only** to infer state:

- `~/.hermes/logs/agent.log` — `conversation turn:` / `API call #` ⇒ thinking;
  `response ready:` ⇒ speaking (4 s) then happy (2 s).
- `~/.hermes/logs/gateway.error.log` — `ERROR` / `RateLimitError` / new
  traceback ⇒ error (6 s).

Priority: error > speaking > happy > thinking > notify > idle. BLE reconnects
with backoff (5 s → 60 s); the daemon never crashes when the panel is off.

Also drives a **Moonside Halo** lamp (`MOONSIDE-*`, Nordic UART) with an effect
per presence state (`lamp.DEFAULT_CONFIG`, editable from the web panel): solid
green idle, BEAT1 cyan thinking, warm white speaking, TWINKLE1 green happy,
BEAT3 red error, WAVE1 purple notify. Separate BLE client from the panel; `--no-lamp` / `--lamp-only` /
env `MOONSIDE_MAC`. Discover by name, never pin UUID.

**THEME protocol:** follow the official reference
(https://developer.moonside.design/) exactly — `THEME.<NAME>.<r,g,b,...>,` with
the documented number of RGB triplets (BEAT1/BEAT3/LAVA1 = 3, WAVE1/PULSING1/
TWINKLE1/GRADIENT1/COLORDROP1/THEME1-5 = 2, GRADIENT2 = 3, FIRE2 = 4, RAINBOW1/2
take a speed, RAINBOW3/FIRE1 take `0`). An unknown name or wrong count hangs the
firmware until power-cycled. `lamp_probe.py` cycles every official theme
(run via launchd through `Murray Lamp.app`, daemon stopped).

**Lamp BLE note:** `--once-demo` / `--lamp-only` leave the Halo on idle mango and
never send `LEDOFF`. Closing the GATT connection may still dim or power off the
lamp (firmware / BLE sleep). Historical `moonside_daemon` sent explicit `LEDOFF`
on exit; this project does not. Keep the daemon running (`drive_lamp` holds the
connection) if you need the lamp to stay lit. Demo default brightness is 100
(range 0–120) so idle is visible in daylight.



## Usage

```bash
# One-time setup (Python >=3.12 required by pyidotmatrix; system python3 is 3.9)
# e.g. uv python: ~/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Tests
.venv/bin/python -m unittest discover -s tests -v

# Preview frames without Bluetooth (PNG + GIF in preview/)
.venv/bin/python avatar.py preview


# Lamp-only dry demo (no panel BLE; logs COLOR commands)
.venv/bin/python presence_daemon.py --once-demo --lamp-only --dry-run

# Dry run: print state transitions from real Hermes logs, no BLE
.venv/bin/python presence_daemon.py --dry-run --from-start --seconds 10

# Physical demo (run from Terminal so macOS grants Bluetooth permission)
.venv/bin/python presence_daemon.py --once-demo --device 1D6E439A-F83F-C1AE-B31B-6F09BBC97163

# Install as a user service (launchd)
cp com.wonderwallit.murray-led-presence.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.wonderwallit.murray-led-presence.plist
# Logs: ~/Library/Logs/murray-led-presence.log

# Lamp-only service (what is actually installed on the Mac Mini; the panel is
# owned by LED Avatar.app). Runs through `Murray Lamp.app`, a bundle whose
# Info.plist carries NSBluetoothAlwaysUsageDescription — a bare venv python
# is killed by TCC (SIGABRT, exit 134) before it can even ask for permission.
cp com.wonderwallit.murray-lamp.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wonderwallit.murray-lamp.plist
# Logs: ~/Library/Logs/murray-lamp.log
```

## Control panel (web)

`panel.py` serves a small page (default `http://localhost:7778`, service
`com.wonderwallit.murray-panel` binds `0.0.0.0` so the phone can open it too)
to pick the effect and colours of every state, a global brightness, and a
**manual** mode that holds one effect regardless of state. **Probar** shows an
effect for 8 s via `~/.murray-lamp/preview.json`; **Guardar** writes
`~/.murray-lamp/config.json`, which `lamp.LampPlanner` hot-reloads (mtime) —
no restart. Effects are validated against `THEME_CATALOG` on both sides so a
malformed theme can never reach the firmware.

```bash
cp com.wonderwallit.murray-panel.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wonderwallit.murray-panel.plist
```

## Claude Code integration

Claude Code hooks call `claude_hook.sh <HookEventName>`, which appends
`<epoch> <HookEventName>` to `~/.murray-lamp/claude-events.log`. The daemon
tails that file like the Hermes logs (see `CLAUDE_HOOK_EVENTS`):

| Hook event | Lamp |
|---|---|
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStart` | cyan (thinking) |
| `Stop` | warm white (speaking) → green (happy) → mango |
| `PostToolUseFailure` | red (error) |
| `Notification`, `PermissionRequest` | purple (waiting for you; overrides thinking) |

Register in `~/.claude/settings.json` one block per event, e.g.:

```json
"Stop": [{"hooks": [{"type": "command",
  "command": "/Users/joss/projects/murray-led-presence/claude_hook.sh Stop"}]}]
```

The hook is a plain zsh script that always exits 0 and resets the file past
1 MB (the tailer treats truncation as rotation).

## Origin

This repository started as a fork of Bobby Bobak's MIT-licensed
[claude-lamp](https://github.com/bobek-balinek/claude-lamp) (Moonside protocol,
`COLOR`/`BRIGH` commands, purple-when-waiting idea). His original scripts and
README are kept in `claude_hooks/` and `README.upstream.md`.

## Attribution & license notes

- `avatar.py` started from Jose's validated led-avatar project
  (`~/Documents/Codex/2026-09-14/referenced-chatgpt-conversation-this-is-an/outputs/led-avatar/`).
- `expressions.py` uses After Dark “Eyes” hollow square frames (CRT cyan/
  yellow-green glow, block pupils, comic mouth); `EXPRESSIONS` API unchanged.
- Depends on [pyidotmatrix](https://github.com/Madhat69/pyidotmatrix)
  (pinned rev `da00f354`), which is licensed under **GPL-3.0**. This project
  inherits that license obligation for distribution.
- Also uses Bleak 3.0.2 and Pillow 12.3.0 (pinned in `requirements.txt`).
