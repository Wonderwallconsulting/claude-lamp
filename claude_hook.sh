#!/bin/sh
# Agent hook (Claude Code, Codex, Cursor — local or on a remote SSH host):
# append "<epoch> <HookEventName> <agent>" for the lamp daemon to tail.
# POSIX sh so it also runs on Linux servers. Always exits 0, never blocks the agent.
# Cursor pipes JSON on stdin and expects it to be consumed.
[ -t 0 ] || cat > /dev/null
DIR="$HOME/.murray-lamp"
FILE="$DIR/claude-events.log"
mkdir -p "$DIR" 2>/dev/null
# LogTail treats truncation as a rotation, so it is safe to reset the file.
if [ -f "$FILE" ] && [ "$(wc -c < "$FILE" 2>/dev/null || echo 0)" -gt 1000000 ]; then : > "$FILE"; fi
printf '%s %s %s\n' "$(date +%s)" "$1" "${2:-claude}" >> "$FILE" 2>/dev/null
exit 0
