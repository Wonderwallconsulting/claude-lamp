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
            "idle": (255, 180, 50),
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

    def test_state_to_theme_commands(self):
        from lamp import STATE_THEME, commands_for_state
        for state in ("idle", "thinking", "happy", "speaking", "error", "notify"):
            cmds = commands_for_state(state, brightness=80)
            self.assertEqual(cmds, ["LEDON", "BRIGH080", STATE_THEME[state]], state)
        self.assertEqual(STATE_THEME["speaking"], "THEME.WAVE1.40,220,80,255,255,255,")
        self.assertEqual(STATE_THEME["notify"], "THEME.WAVE1.200,0,255,255,255,255,")

    def test_theme_param_counts_match_official_docs(self):
        # developer.moonside.design: BEAT1/BEAT3 take 3 RGB, WAVE1/PULSING1/TWINKLE1 take 2.
        from lamp import STATE_THEME
        expected_colors = {"BEAT1": 3, "BEAT3": 3, "WAVE1": 2, "PULSING1": 2, "TWINKLE1": 2}
        for state, cmd in STATE_THEME.items():
            self.assertTrue(cmd.startswith("THEME.") and cmd.endswith(","), cmd)
            name, params = cmd[len("THEME."):].split(".", 1)
            values = [v for v in params.split(",") if v]
            self.assertEqual(len(values), 3 * expected_colors[name], f"{state}: {cmd}")

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
