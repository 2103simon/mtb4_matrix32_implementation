"""Guided console output contracts, without terminals, timers or devices."""
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from oscilloscope.guided_console import GuidedConsole, _cells


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def frame(*, index=0, text="HOLD steady", elapsed=0.0, next_in=3.0,
          remaining=20.0, record=1):
    return {
        "record_index": record, "record_count": 1,
        "elapsed_s": elapsed, "duration_s": 20.0,
        "text": text, "cue_index": index,
        "next_in_s": next_in, "next_text": "RELEASE completely",
        "remaining_s": remaining,
    }


def visible_screen(output):
    """Interpret the small ANSI subset used by the renderer, independently."""
    rows = [""]
    row = col = 0
    parts = re.split(r"(\x1b\[[0-9;]*[A-Za-z])", output)
    for part in parts:
        if part.startswith("\x1b["):
            if part.endswith("A"):
                row = max(0, row - int(part[2:-1] or "1"))
            elif part.endswith("K"):
                rows[row] = ""
            continue
        for char in part:
            if char == "\n":
                row += 1
                col = 0
                while len(rows) <= row:
                    rows.append("")
            elif char == "\r":
                col = 0
            else:
                existing = rows[row].ljust(col)
                rows[row] = existing[:col] + char + existing[col + 1:]
                col += 1
    return "\n".join(rows)


class GuidedConsoleTests(unittest.TestCase):
    def test_quiet_capture_logs_full_diagnostics_but_exposes_critical_lines(self):
        terminal = io.StringIO()
        console = GuidedConsole(terminal)
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "console.log"
            with console.capture(log):
                print('{"schema_version": 1, "points": 10000000}')
                print("[MCU] configured")
                print("[FRAME EVIDENCE WARN] incomplete frame data")
                print("[MATRIX32] [ERROR] controller fault")
                print("[SAFETY] Verify physical output off")
                print("serial exception detail", file=sys.stderr)
                console.log_prompt("[PROMPT record 1/1 +0.0s] HOLD")
                console.show_prompt(frame())
            contents = log.read_text()
        display = terminal.getvalue()
        self.assertNotIn('"schema_version"', display)
        self.assertNotIn("[MCU] configured", display)
        for text in ("incomplete frame data", "controller fault",
                     "Verify physical output off", "serial exception detail", "HOLD steady"):
            self.assertIn(text, display)
        self.assertIn('{"schema_version": 1, "points": 10000000}\n', contents)
        self.assertIn("[MCU] configured\n", contents)
        self.assertEqual(contents.count("[PROMPT record 1/1 +0.0s] HOLD"), 1)
        self.assertNotIn("GUIDED RECORD", contents)

    def test_verbose_preserves_diagnostics_and_avoids_cursor_control(self):
        terminal = FakeTTY()
        console = GuidedConsole(terminal, verbose=True)
        with tempfile.TemporaryDirectory() as folder:
            with console.capture(Path(folder) / "console.log"):
                print("routine acquisition diagnostics")
                print('  "message": "two  spaces"')
                console.show_prompt(frame())
                console.show_prompt(frame(elapsed=0.2, next_in=2.8, remaining=19.8))
        display = terminal.getvalue()
        self.assertIn("routine acquisition diagnostics", display)
        self.assertIn('  "message": "two  spaces"\n', display)
        self.assertEqual(display.count("HOLD steady"), 1)
        self.assertNotIn("\x1b[", display)

    def test_non_tty_prints_once_per_cue_without_countdown_spam(self):
        terminal = io.StringIO()
        console = GuidedConsole(terminal)
        for second in range(3):
            console.show_prompt(frame(elapsed=second, next_in=3-second,
                                      remaining=20-second))
        console.show_prompt(frame(index=1, text="RELEASE", elapsed=3,
                                  next_in=3, remaining=17))
        console.show_prompt(frame(index=3, text="RELEASE", elapsed=10,
                                  next_in=3, remaining=10))
        display = terminal.getvalue()
        self.assertEqual(display.count("HOLD steady"), 1)
        self.assertEqual(display.count("RELEASE\n"), 2)
        self.assertEqual(display.count("DO NOW"), 3)

    def test_tty_refresh_replaces_current_action_and_deduplicates_same_second(self):
        terminal = FakeTTY()
        console = GuidedConsole(terminal)
        with patch("oscilloscope.guided_console.shutil.get_terminal_size",
                   return_value=os.terminal_size((80, 24))):
            console.show_prompt(frame())
            initial = terminal.getvalue()
            console.show_prompt(frame(elapsed=0.2, next_in=2.8, remaining=19.8))
            self.assertEqual(terminal.getvalue(), initial)
            console.show_prompt(frame(index=1, text="RELEASE NOW", elapsed=3,
                                      next_in=3, remaining=17))
            screen = visible_screen(terminal.getvalue())
        self.assertIn("RELEASE NOW", screen)
        self.assertNotIn("HOLD steady", screen)
        self.assertIn("17s REMAINING", screen)
        self.assertIn("NEXT IN 3s", screen)

    def test_warnings_remain_visible_above_replaced_prompt_frames(self):
        terminal = FakeTTY()
        console = GuidedConsole(terminal)
        with tempfile.TemporaryDirectory() as folder:
            with console.capture(Path(folder) / "console.log"):
                console.show_prompt(frame())
                print("[WARNING] Keep this diagnostic visible")
                console.show_prompt(frame(index=1, text="RELEASE NOW", elapsed=3,
                                          next_in=3, remaining=17))
                console.status("CAPTURE FINISHED", "Downloading the frozen waveforms")
        screen = visible_screen(terminal.getvalue())
        self.assertIn("Keep this diagnostic visible", screen)
        self.assertIn("CAPTURE FINISHED", screen)
        self.assertNotIn("HOLD steady", screen)
        self.assertNotIn("RELEASE NOW", screen)

    def test_tty_frames_fit_width_and_use_at_most_four_rows(self):
        terminal = FakeTTY()
        console = GuidedConsole(terminal)
        with patch("oscilloscope.guided_console.shutil.get_terminal_size",
                   return_value=os.terminal_size((30, 24))):
            console.show_prompt(frame(text="PRESS 非常 gently while maintaining the specified contact"))
        # Style codes do not occupy terminal cells. A spare final column avoids
        # terminal auto-wrap and leaves cursor-up operations predictable.
        lines = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", terminal.getvalue()).splitlines()
        self.assertLessEqual(len(lines), 4)
        self.assertTrue(all(_cells(line) <= 29 for line in lines), lines)

    def test_capture_restores_both_streams_and_flushes_partial_output_on_error(self):
        outer_stdout, outer_stderr = io.StringIO(), io.StringIO()
        terminal = io.StringIO()
        console = GuidedConsole(terminal)
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "console.log"
            with redirect_stdout(outer_stdout), redirect_stderr(outer_stderr):
                with self.assertRaisesRegex(RuntimeError, "test failure"):
                    with console.capture(log):
                        sys.stdout.write("unfinished diagnostic")
                        raise RuntimeError("test failure")
                self.assertIs(sys.stdout, outer_stdout)
                self.assertIs(sys.stderr, outer_stderr)
                print("after capture")
            self.assertEqual(log.read_text(), "unfinished diagnostic")
        self.assertEqual(outer_stdout.getvalue(), "after capture\n")

    def test_relay_thread_diagnostics_are_saved_and_warning_survives(self):
        terminal = FakeTTY()
        console = GuidedConsole(terminal)
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "console.log"
            with console.capture(log):
                console.show_prompt(frame())
                def relay():
                    for index in range(30):
                        print(f"[MCU] message {index}", flush=True)
                    print("[ERROR] relay reported failure", flush=True)
                worker = threading.Thread(target=relay)
                worker.start()
                for second in range(3):
                    console.show_prompt(frame(elapsed=second, next_in=3-second,
                                              remaining=20-second))
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())
            contents = log.read_text()
        self.assertEqual(contents.count("[MCU] message"), 30)
        screen = visible_screen(terminal.getvalue())
        self.assertIn("relay reported failure", screen)
        self.assertNotIn("[MCU] message", screen)


if __name__ == "__main__":
    unittest.main()
