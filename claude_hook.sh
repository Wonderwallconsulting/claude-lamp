#!/bin/zsh
# Agent hook (Claude Code, Codex, Cursor): append "<epoch> <HookEventName> <agent>"
# for the lamp daemon to tail. Always exits 0 and never blocks the agent.
# Cursor pipes JSON on stdin and expects it to be consumed.
[ -t 0 ] || cat > /dev/null
DIR="$HOME/.murray-lamp"
FILE="$DIR/claude-events.log"
mkdir -p "$DIR" 2>/dev/null
# LogTail treats truncation as a rotation, so it is safe to reset the file.
[ -f "$FILE" ] && [ "$(stat -f%z "$FILE" 2>/dev/null || echo 0)" -gt 1000000 ] && : > "$FILE"
printf '%s %s %s\n' "$(date +%s)" "$1" "${2:-claude}" >> "$FILE" 2>/dev/null
exit 0
