#!/usr/bin/env python3
"""Murray Lamp control panel — local web UI to pick effects/colours per state.

Talks to the daemon only through files: ~/.murray-lamp/config.json (saved
settings, hot-reloaded by LampPlanner) and preview.json ("Probar" button).
No BLE here, so it runs anywhere without the TCC app bundle.
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lamp import (CONFIG_PATH, DEFAULT_CONFIG, PREVIEW_PATH, PREVIEW_SECONDS,
                  THEME_CATALOG, _valid_effect, load_config, save_config)

HTML_PATH = Path(__file__).with_name("panel.html")
LOG_PATH = Path.home() / "Library/Logs/murray-lamp.log"
STATE_LABELS = {
    "idle": "Reposo", "thinking": "Pensando", "speaking": "Hablando",
    "happy": "Contento", "error": "Error", "notify": "Esperándote",
}


def sanitize_config(raw: dict) -> dict:
    """Keep only valid effects for known states; never trust the browser."""
    cfg = load_config(Path("/nonexistent"))  # defaults
    if isinstance(raw.get("brightness"), (int, float)):
        cfg["brightness"] = max(0, min(120, int(raw["brightness"])))
    cfg["manual"] = raw["manual"] if _valid_effect(raw.get("manual")) else None
    for state, effect in (raw.get("states") or {}).items():
        if state in cfg["states"] and _valid_effect(effect):
            cfg["states"][state] = effect
    return cfg


def recent_log(n: int = 6) -> list[str]:
    try:
        lines = LOG_PATH.read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [l for l in lines if "TX " in l or "state ->" in l][-n:]


class Handler(BaseHTTPRequestHandler):
    config_path = CONFIG_PATH
    preview_path = PREVIEW_PATH

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/":
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/config":
            self._json(200, {
                "config": load_config(self.config_path),
                "defaults": DEFAULT_CONFIG,
                "catalog": THEME_CATALOG,
                "labels": STATE_LABELS,
                "preview_seconds": PREVIEW_SECONDS,
            })
        elif self.path == "/api/status":
            self._json(200, {"log": recent_log()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/config":
            cfg = sanitize_config(self._body())
            save_config(cfg, self.config_path)
            self._json(200, {"ok": True, "config": cfg})
        elif self.path == "/api/preview":
            effect = self._body().get("effect")
            if not _valid_effect(effect):
                self._json(400, {"ok": False, "error": "efecto no válido"})
                return
            self.preview_path.parent.mkdir(parents=True, exist_ok=True)
            self.preview_path.write_text(json.dumps({"ts": time.time(), "effect": effect}))
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):  # quieter launchd log
        if "/api/status" not in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (0.0.0.0 to reach it from the phone)")
    p.add_argument("--port", type=int, default=7778)
    args = p.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Murray Lamp panel: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
