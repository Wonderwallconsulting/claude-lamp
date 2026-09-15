"""Unit tests: log parsers with REAL Hermes log samples + state machine."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from presence_daemon import (StateMachine, parse_agent_line,
                             parse_gateway_error_line, parse_claude_event_line,
                             observe, LogTail, NOTIFY_SECONDS)

# Real lines copied verbatim from /Users/joss/.hermes/logs/agent.log
REAL_TURN = ("2026-09-14 08:15:52,948 INFO [cron_002c42c6275e_20260914_081552] "
             "agent.turn_context: conversation turn: session=cron_002c42c6275e_20260914_081552 "
             "model=claude-fable-5 provider=anthropic platform=cron history=0 "
             "msg='[IMPORTANT: The user has invoked the \"chefoach-healthkit\" skill, indicating they...'")
REAL_API_CALL = ("2026-09-14 04:45:36,607 INFO [cron_57be73c12bbd_20260914_044530] "
                 "agent.conversation_loop: API call #1: model=gpt-5.6-terra provider=openai-codex "
                 "in=15717 out=132 total=15849 latency=4.3s "
                 "id=resp_08f32b4ff89ccb83016aa75fcce2e887d29217874ab8ece253")
REAL_RESPONSE = ("2026-09-14 08:21:33,082 INFO gateway.run: response ready: platform=telegram "
                 "chat=13726908 time=115.4s api_calls=12 response=1104 chars")
REAL_SILENT = ("2026-09-14 17:30:40,216 INFO cron.scheduler: Job '29d2928926d4': "
               "agent returned [SILENT] — skipping delivery")

# Real lines copied verbatim from /Users/joss/.hermes/logs/gateway.error.log
REAL_RATELIMIT = ("2026-09-07 08:13:25,406 anthropic.RateLimitError: Error code: 429 - "
                  "{'type': 'error', 'error': {'type': 'rate_limit_error', 'message': "
                  "\"This request would exceed your account's rate limit. Please try again later.\"}, "
                  "'request_id': 'req_011CeoawjKsWfDHi2ejznr7w'}")
REAL_ERROR = ("2026-08-18 01:00:45,644 ERROR gateway.platforms.base: [Telegram] Telegram bot token "
              "already in use by the 'hermes-improver' profile gateway (PID 4767).")
REAL_TRACEBACK = "Traceback (most recent call last):"
REAL_WARNING = ("2026-09-14 15:30:18,689 WARNING tools.registry: check_fn check_browser_requirements "
                "returned False; dependent tools will be unavailable this turn")


class TestParsers(unittest.TestCase):
    def test_conversation_turn_is_thinking(self):
        self.assertEqual(parse_agent_line(REAL_TURN), "thinking")

    def test_api_call_is_thinking(self):
        self.assertEqual(parse_agent_line(REAL_API_CALL), "thinking")

    def test_response_ready_is_speaking(self):
        self.assertEqual(parse_agent_line(REAL_RESPONSE), "speaking")

    def test_silent_cron_noise_ignored(self):
        self.assertIsNone(parse_agent_line(REAL_SILENT))

    def test_ratelimit_is_error(self):
        self.assertEqual(parse_gateway_error_line(REAL_RATELIMIT), "error")

    def test_error_line_is_error(self):
        self.assertEqual(parse_gateway_error_line(REAL_ERROR), "error")

    def test_traceback_is_error(self):
        self.assertEqual(parse_gateway_error_line(REAL_TRACEBACK), "error")

    def test_warning_is_not_error(self):
        self.assertIsNone(parse_gateway_error_line(REAL_WARNING))


class TestStateMachine(unittest.TestCase):
    def test_idle_by_default(self):
        self.assertEqual(StateMachine().current(0.0), "idle")

    def test_thinking_then_expires(self):
        m = StateMachine()
        m.feed("thinking", 0.0)
        self.assertEqual(m.current(1.0), "thinking")
        self.assertEqual(m.current(25.0), "idle")

    def test_speaking_then_happy_then_idle(self):
        m = StateMachine()
        m.feed("speaking", 0.0)
        self.assertEqual(m.current(1.0), "speaking")
        self.assertEqual(m.current(4.5), "happy")
        self.assertEqual(m.current(7.0), "idle")

    def test_speaking_clears_thinking(self):
        m = StateMachine()
        m.feed("thinking", 0.0)
        m.feed("speaking", 1.0)
        self.assertEqual(m.current(6.0), "happy")  # not thinking

    def test_error_beats_everything(self):
        m = StateMachine()
        m.feed("thinking", 0.0)
        m.feed("speaking", 0.0)
        m.feed("notify", 0.0)
        m.feed("error", 0.0)
        self.assertEqual(m.current(1.0), "error")
        self.assertEqual(m.current(6.5), "idle")  # error 6s, speaking window already gone at 6.5? no:
        # speaking expired at 4, happy at 6 -> idle at 6.5. Correct.

    def test_speaking_beats_thinking_and_notify(self):
        m = StateMachine()
        m.feed("notify", 0.0)
        m.feed("speaking", 0.0)
        self.assertEqual(m.current(1.0), "speaking")

    def test_thinking_beats_notify(self):
        m = StateMachine()
        m.feed("notify", 0.0)
        m.feed("thinking", 0.0)
        self.assertEqual(m.current(1.0), "thinking")

    def test_notify_then_idle(self):
        m = StateMachine()
        m.feed("notify", 0.0)
        self.assertEqual(m.current(1.0), "notify")
        self.assertEqual(m.current(4.0), "idle")

    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            StateMachine().feed("dancing", 0.0)


class TestClaudeEvents(unittest.TestCase):
    """Lines appended by claude_hook.sh: '<epoch> <HookEventName>'."""

    def test_prompt_and_tools_are_thinking(self):
        for name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart"):
            self.assertEqual(parse_claude_event_line(f"1757950000 {name}"), "thinking", name)

    def test_stop_is_speaking(self):
        self.assertEqual(parse_claude_event_line("1757950000 Stop"), "speaking")

    def test_tool_failure_is_error(self):
        self.assertEqual(parse_claude_event_line("1757950000 PostToolUseFailure"), "error")

    def test_waiting_for_user_is_waiting(self):
        for name in ("Notification", "PermissionRequest"):
            self.assertEqual(parse_claude_event_line(f"1757950000 {name}"), "waiting", name)

    def test_waiting_interrupts_thinking_with_notify(self):
        m = StateMachine()
        m.feed("thinking", 0.0)
        m.feed("waiting", 1.0)
        self.assertEqual(m.current(1.5), "notify")
        self.assertEqual(m.current(1.0 + NOTIFY_SECONDS + 0.1), "idle")

    def test_unknown_or_malformed_ignored(self):
        self.assertIsNone(parse_claude_event_line("1757950000 SessionEnd"))
        self.assertIsNone(parse_claude_event_line("garbage"))
        self.assertIsNone(parse_claude_event_line(""))

    def test_observe_reacts_to_claude_events_file(self):
        import asyncio, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            claude = Path(tmp) / "claude-events.log"
            claude.write_text("")
            seen = []
            machine = StateMachine()

            async def run():
                task = asyncio.create_task(observe(
                    machine, lambda s, l: seen.append(s),
                    agent_log=Path(tmp) / "agent.log",
                    gateway_log=Path(tmp) / "gw.log",
                    claude_log=claude, poll_interval=0.05, stop_after=0.6))
                await asyncio.sleep(0.15)
                with open(claude, "a") as fh:
                    fh.write("1757950000 PreToolUse\n")
                await task

            asyncio.run(run())
            self.assertIn("thinking", seen)


class TestLogTail(unittest.TestCase):
    def test_tail_reads_only_new_lines_and_handles_rotation(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            path.write_text("old line\n")
            tail = LogTail(path)
            self.assertEqual(tail.poll(), [])  # starts at end
            with open(path, "a") as fh:
                fh.write("new line 1\nnew line 2\npartial")
            self.assertEqual(tail.poll(), ["new line 1", "new line 2"])
            self.assertEqual(tail.poll(), [])  # partial line held back
            # Rotation: replace the file
            os.rename(path, Path(tmp) / "x.log.1")
            path.write_text("after rotate\n")
            self.assertEqual(tail.poll(), ["after rotate"])

    def test_from_start_replays_existing_content(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            path.write_text("a\nb\n")
            tail = LogTail(path, from_start=True)
            self.assertEqual(tail.poll(), ["a", "b"])

    def test_missing_file_is_not_an_error(self):
        tail = LogTail(Path("/nonexistent/never.log"))
        self.assertEqual(tail.poll(), [])




class TestExpressions(unittest.TestCase):
    def test_all_expressions_render_32(self):
        from expressions import EXPRESSIONS, render
        for name in EXPRESSIONS:
            img = render(name, 0)
            self.assertEqual(img.size, (32, 32))
            self.assertEqual(img.mode, "RGB")

    def test_thinking_phase_changes_pixels(self):
        from expressions import render
        a = list(render("thinking", 0).getdata())
        b = list(render("thinking", 1).getdata())
        self.assertNotEqual(a, b)

    def test_idle_phase_changes_pixels(self):
        from expressions import render
        a = list(render("idle", 0).getdata())
        b = list(render("idle", 1).getdata())
        self.assertNotEqual(a, b)

    def test_unknown_raises(self):
        from expressions import render
        with self.assertRaises(ValueError):
            render("nope")


class TestLamp(unittest.TestCase):
    def test_state_to_color_mapping(self):
        from lamp import STATE_RGB, build_color_cmd, color_for_state, commands_for_state
        expected = {
            "idle": (40, 220, 80),
            "thinking": (0, 220, 255),
            "happy": (40, 220, 80),
            "speaking": (255, 240, 220),
            "error": (255, 40, 40),
            "notify": (200, 0, 255),
        }
        self.assertEqual(STATE_RGB, expected)
        for state, rgb in expected.items():
            self.assertEqual(color_for_state(state), rgb)
            self.assertEqual(build_color_cmd(*rgb), f"COLOR{rgb[0]:03d}{rgb[1]:03d}{rgb[2]:03d}")
            self.assertEqual(commands_for_state(state)[-1][:5] in ("COLOR", "THEME"), True)

    def test_default_state_commands(self):
        from lamp import commands_for_state
        self.assertEqual(commands_for_state("idle", brightness=80),
                         ["LEDON", "BRIGH080", "COLOR040220080"])
        self.assertEqual(commands_for_state("notify", brightness=80),
                         ["LEDOFF", "LEDON", "BRIGH080", "THEME.WAVE1.200,0,255,255,255,255,"])
        self.assertEqual(commands_for_state("thinking", brightness=80)[-1],
                         "THEME.BEAT1.0,220,255,0,0,140,255,255,255,")

    def test_build_theme_cmd_follows_official_catalog(self):
        # developer.moonside.design: BEAT1 = 3 RGB, WAVE1 = 2, FIRE2 = 4, RAINBOW1 = speed, FIRE1 = none.
        from lamp import THEME_CATALOG, build_theme_cmd
        self.assertEqual(THEME_CATALOG["BEAT1"], 3)
        self.assertEqual(THEME_CATALOG["FIRE2"], 4)
        self.assertEqual(build_theme_cmd("WAVE1", [(1, 2, 3), (4, 5, 6)]), "THEME.WAVE1.1,2,3,4,5,6,")
        self.assertEqual(build_theme_cmd("RAINBOW1", [], speed=20), "THEME.RAINBOW1.20,")
        self.assertEqual(build_theme_cmd("FIRE1", []), "THEME.FIRE1.0,")
        self.assertEqual(build_theme_cmd("fire1", [(9, 9, 9)]), "THEME.FIRE1.0,")  # extras ignored
        with self.assertRaises(ValueError):
            build_theme_cmd("WAVE1", [(1, 2, 3)])  # too few colours hangs the firmware
        with self.assertRaises(ValueError):
            build_theme_cmd("BREATH1", [(1, 2, 3)])  # not a real theme

    def test_effect_commands_solid_and_theme(self):
        from lamp import effect_commands
        self.assertEqual(effect_commands({"theme": None, "colors": [[1, 2, 3]]}, brightness=50),
                         ["LEDON", "BRIGH050", "COLOR001002003"])
        self.assertEqual(effect_commands({"theme": "FIRE1", "colors": []}, brightness=50),
                         ["LEDOFF", "LEDON", "BRIGH050", "THEME.FIRE1.0,"])

    def test_config_roundtrip_and_override(self):
        import json, tempfile
        from lamp import DEFAULT_CONFIG, commands_for_state, load_config, save_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = load_config(path)  # missing file -> defaults
            self.assertEqual(cfg, DEFAULT_CONFIG)
            cfg["states"]["idle"] = {"theme": "PULSING1", "colors": [[1, 1, 1], [2, 2, 2]]}
            cfg["brightness"] = 60
            save_config(cfg, path)
            cfg2 = load_config(path)
            self.assertEqual(commands_for_state("idle", config=cfg2),
                             ["LEDOFF", "LEDON", "BRIGH060", "THEME.PULSING1.1,1,1,2,2,2,"])
            # unknown states in the file are ignored, missing ones fall back to defaults
            path.write_text(json.dumps({"states": {"bogus": {"theme": None, "colors": [[0, 0, 0]]}}}))
            cfg3 = load_config(path)
            self.assertEqual(cfg3["states"]["thinking"], DEFAULT_CONFIG["states"]["thinking"])
            self.assertNotIn("bogus", cfg3["states"])
            # corrupt file -> defaults, never raises
            path.write_text("{not json")
            self.assertEqual(load_config(path), DEFAULT_CONFIG)

    def test_planner_reload_preview_and_manual(self):
        import json, os, tempfile, time
        from lamp import LampPlanner, PREVIEW_SECONDS, save_config, load_config
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path, prev_path = Path(tmp) / "config.json", Path(tmp) / "preview.json"
            p = LampPlanner(config_path=cfg_path, preview_path=prev_path)
            # first call applies, repeated state is a no-op
            self.assertEqual(p.plan("idle", 0.0)[-1], "COLOR040220080")
            self.assertIsNone(p.plan("idle", 1.0))
            self.assertEqual(p.plan("thinking", 2.0)[-1][:11], "THEME.BEAT1")
            # config change is picked up without restart and re-applied
            cfg = load_config(cfg_path)
            cfg["states"]["thinking"] = {"theme": "FIRE1", "colors": []}
            save_config(cfg, cfg_path)
            os.utime(cfg_path, (time.time() + 5, time.time() + 5))  # ensure mtime differs
            self.assertEqual(p.plan("thinking", 3.0)[-1], "THEME.FIRE1.0,")
            # preview wins for PREVIEW_SECONDS, then the state comes back
            prev_path.write_text(json.dumps({"ts": time.time(),
                                             "effect": {"theme": "RAINBOW1", "colors": [], "speed": 30}}))
            self.assertEqual(p.plan("thinking", 4.0)[-1], "THEME.RAINBOW1.30,")
            self.assertIsNone(p.plan("idle", 5.0))  # state changes are held during preview
            self.assertEqual(p.plan("idle", 4.0 + PREVIEW_SECONDS + 0.1)[-1], "COLOR040220080")
            # stale preview file is ignored
            prev_path.write_text(json.dumps({"ts": time.time() - 3600,
                                             "effect": {"theme": "FIRE1", "colors": []}}))
            self.assertIsNone(p.plan("idle", 20.0))
            # manual mode holds one effect regardless of state
            cfg["manual"] = {"theme": None, "colors": [[9, 9, 9]]}
            save_config(cfg, cfg_path)
            os.utime(cfg_path, (time.time() + 10, time.time() + 10))
            self.assertEqual(p.plan("error", 21.0)[-1], "COLOR009009009")
            self.assertIsNone(p.plan("thinking", 22.0))

    def test_manual_mode_overrides_state(self):
        from lamp import DEFAULT_CONFIG, commands_for_state
        import copy
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["manual"] = {"theme": "FIRE1", "colors": []}
        self.assertEqual(commands_for_state("thinking", config=cfg)[-1], "THEME.FIRE1.0,")
        self.assertEqual(commands_for_state("blink", config=cfg), [])

    def test_blink_wink_do_not_change_lamp(self):
        from lamp import color_for_state, commands_for_state
        self.assertIsNone(color_for_state("blink"))
        self.assertIsNone(color_for_state("wink"))
        self.assertEqual(commands_for_state("blink"), [])
        self.assertEqual(commands_for_state("wink"), [])

    def test_backoff_never_raises_on_missing_device(self):
        """drive_lamp soft-fails when discovery returns None; backoff does not crash."""
        import asyncio
        from unittest.mock import AsyncMock, patch
        from lamp import drive_lamp

        class FakeMachine:
            def current(self, now):
                return "idle"

        async def run():
            with patch("lamp.discover_moonside", new_callable=AsyncMock) as disc:
                disc.return_value = None
                sleeps = []

                async def fake_sleep(secs):
                    sleeps.append(secs)
                    if len(sleeps) >= 3:
                        raise asyncio.CancelledError()

                with patch("lamp.asyncio.sleep", side_effect=fake_sleep):
                    try:
                        await drive_lamp(FakeMachine(), dry_run=False, poll_interval=0.01)
                    except asyncio.CancelledError:
                        pass
                self.assertGreaterEqual(len(sleeps), 3)
                # First three backoffs: 5, 10, 20 (doubling toward 60).
                self.assertEqual(sleeps[:3], [5.0, 10.0, 20.0])

        asyncio.run(run())

    def test_dry_run_does_not_touch_ble(self):
        import asyncio
        from unittest.mock import AsyncMock, patch, MagicMock
        from lamp import drive_lamp, demo_cycle

        class FakeMachine:
            def __init__(self):
                self._n = 0

            def current(self, now):
                self._n += 1
                return "thinking" if self._n == 1 else "idle"

        async def run_drive():
            with patch("lamp.discover_moonside", new_callable=AsyncMock) as disc, \
                 patch.dict("sys.modules", {"bleak": MagicMock()}):
                sleeps = 0

                async def fake_sleep(secs):
                    nonlocal sleeps
                    sleeps += 1
                    if sleeps >= 2:
                        raise asyncio.CancelledError()

                with patch("lamp.asyncio.sleep", side_effect=fake_sleep):
                    try:
                        await drive_lamp(FakeMachine(), dry_run=True, poll_interval=0.01)
                    except asyncio.CancelledError:
                        pass
                disc.assert_not_called()

        async def run_demo():
            with patch("lamp.discover_moonside", new_callable=AsyncMock) as disc:
                await demo_cycle(dry_run=True, hold=0.0)
                disc.assert_not_called()

        asyncio.run(run_drive())
        asyncio.run(run_demo())


class TestPanel(unittest.TestCase):
    def test_panel_api_roundtrip(self):
        import json, tempfile, threading, urllib.request
        from http.server import ThreadingHTTPServer
        import panel
        with tempfile.TemporaryDirectory() as tmp:
            class H(panel.Handler):
                config_path = Path(tmp) / "config.json"
                preview_path = Path(tmp) / "preview.json"
            srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_port}"
            try:
                def call(path, body=None):
                    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None,
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                self.assertIn(b"Murray Lamp", urllib.request.urlopen(base + "/").read())
                status, data = call("/api/config")
                self.assertEqual(status, 200)
                self.assertIn("BEAT1", data["catalog"])
                # bad theme is dropped, good one is saved
                cfg = data["config"]
                cfg["states"]["idle"] = {"theme": "NOPE", "colors": []}
                cfg["states"]["error"] = {"theme": "FIRE1", "colors": []}
                cfg["brightness"] = 42
                status, saved = call("/api/config", cfg)
                self.assertEqual(saved["config"]["states"]["idle"], data["defaults"]["states"]["idle"])
                self.assertEqual(saved["config"]["states"]["error"]["theme"], "FIRE1")
                self.assertEqual(json.loads(H.config_path.read_text())["brightness"], 42)
                # preview writes a fresh timestamped file; invalid effect is refused
                status, _ = call("/api/preview", {"effect": {"theme": "WAVE1", "colors": [[1,2,3],[4,5,6]]}})
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(H.preview_path.read_text())["effect"]["theme"], "WAVE1")
                with self.assertRaises(urllib.error.HTTPError):
                    call("/api/preview", {"effect": {"theme": "WAVE1", "colors": [[1,2,3]]}})
            finally:
                srv.shutdown()
