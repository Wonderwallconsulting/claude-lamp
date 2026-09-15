#!/bin/zsh
# Idempotent setup of Murray Lamp on a Mac: venv, TCC app bundle, launchd
# services, agent hooks (Claude Code / Codex / Cursor) and config restore.
# Re-run after a migration or a fresh clone:  ./install.sh
set -euo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
HOOK="$PROJ/claude_hook.sh"
AGENTS="$HOME/Library/LaunchAgents"
MIRROR="$HOME/Library/CloudStorage/OneDrive-Personal/Documentos/Cursor projects/Murray lamp/config.json"
UID_="$(id -u)"

say() { print -P "%F{green}==>%f $*"; }

# 1. Python >= 3.12 (pyidotmatrix needs it; system python3 is 3.9)
if [ ! -x "$PROJ/.venv/bin/python" ]; then
  PY="$(command -v python3.12 || true)"
  [ -z "$PY" ] && PY="$(ls -d "$HOME"/.local/share/uv/python/cpython-3.12*/bin/python3.12 2>/dev/null | tail -1)"
  if [ -z "$PY" ]; then
    echo "Falta Python 3.12. Instálalo con: uv python install 3.12   (o brew install python@3.12)" >&2
    exit 1
  fi
  say "Creando venv con $PY"
  "$PY" -m venv "$PROJ/.venv"
fi
say "Instalando dependencias"
"$PROJ/.venv/bin/pip" install -q -r "$PROJ/requirements.txt"

# 2. Executables (git does not always keep the bit on the .app scripts)
chmod +x "$HOOK" "$PROJ/Murray Lamp.app/Contents/MacOS/"*

# 3. Config: restore from the synced mirror if there is no local one
mkdir -p "$HOME/.murray-lamp"
if [ ! -f "$HOME/.murray-lamp/config.json" ] && [ -f "$MIRROR" ]; then
  say "Restaurando config desde OneDrive"
  cp "$MIRROR" "$HOME/.murray-lamp/config.json"
fi

# 4. launchd services (lamp daemon runs through the TCC bundle; panel is plain python)
for label in com.wonderwallit.murray-lamp com.wonderwallit.murray-panel; do
  say "Servicio $label"
  cp "$PROJ/$label.plist" "$AGENTS/"
  if launchctl print "gui/$UID_/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
    for _ in {1..20}; do launchctl print "gui/$UID_/$label" >/dev/null 2>&1 || break; sleep 0.5; done
  fi
  for i in {1..5}; do
    launchctl bootstrap "gui/$UID_" "$AGENTS/$label.plist" 2>/dev/null && break
    [ "$i" = 5 ] && { echo "No se pudo arrancar $label" >&2; exit 1; }
    sleep 1
  done
done

# 5. Agent hooks (merged, never duplicated; backups next to each file)
say "Hooks de Claude Code / Codex / Cursor"
"$PROJ/.venv/bin/python" - "$HOOK" <<'EOF'
import json, sys, time
from pathlib import Path

hook = sys.argv[1]
stamp = time.strftime("%Y%m%dT%H%M%S")
home = Path.home()


def backup(p):
    if p.exists():
        p.with_name(p.name + f".murray-lamp-backup-{stamp}").write_bytes(p.read_bytes())


def merge_claude_style(path, events, agent):
    """~/.claude/settings.json and ~/.codex/hooks.json share the same schema."""
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    changed = False
    for ev in events:
        blocks = hooks.setdefault(ev, [])
        cmd = f"{hook} {ev} {agent}"
        found = False
        for b in blocks:
            for h in b.get("hooks", []):
                if hook in h.get("command", ""):
                    found = True
                    if h["command"] != cmd:
                        h["command"] = cmd
                        changed = True
        if not found:
            blocks.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
            changed = True
    if changed:
        backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"  {path}: {'actualizado' if changed else 'ya estaba'}")


CLAUDE_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart",
                 "Stop", "PostToolUseFailure", "Notification", "PermissionRequest"]
merge_claude_style(home / ".claude/settings.json", CLAUDE_EVENTS, "claude")

CODEX_EVENTS = ["UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
                "Stop", "PostToolUseFailure", "Notification"]
if (home / ".codex").is_dir():
    merge_claude_style(home / ".codex/hooks.json", CODEX_EVENTS, "codex")
    cfg = home / ".codex/config.toml"
    if cfg.exists():
        txt = cfg.read_text()
        if "hooks = true" not in txt:
            backup(cfg)
            txt = txt.replace("codex_hooks = true", "hooks = true")
            if "hooks = true" not in txt:
                txt += "\n[features]\nhooks = true\n"
            cfg.write_text(txt)
            print(f"  {cfg}: hooks habilitados")

# Cursor: flat schema, observational hooks only (never permission hooks)
CURSOR_EVENTS = ["afterAgentThought", "postToolUse", "afterFileEdit", "afterShellExecution",
                 "afterMCPExecution", "postToolUseFailure", "afterAgentResponse", "stop"]
if (home / ".cursor").is_dir():
    path = home / ".cursor/hooks.json"
    data = json.loads(path.read_text()) if path.exists() else {"version": 1, "hooks": {}}
    hooks = data.setdefault("hooks", {})
    changed = False
    for ev in CURSOR_EVENTS:
        cmd = f"{hook} {ev} cursor"
        lst = hooks.setdefault(ev, [])
        if not any(hook in h.get("command", "") for h in lst):
            lst.append({"command": cmd, "timeout": 5})
            changed = True
    if changed:
        backup(path)
        path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"  {path}: {'actualizado' if changed else 'ya estaba'}")
EOF

say "Listo. Panel: http://localhost:7778  ·  Log: ~/Library/Logs/murray-lamp.log"
say "Si macOS pide permiso de Bluetooth para 'Murray Lamp', acéptalo."
