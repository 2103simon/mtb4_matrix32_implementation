"""Quiet recording diagnostics and a persistent, human-facing cue display.

The display has no timer or device access. The caller supplies prompt frames
and owns the lifetime of any scheduler producing them.
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import math
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import unicodedata


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_IMPORTANT = re.compile(r"\b(?:ERROR|WARNING|WARN|SAFETY|FAILED|FAILURE)\b", re.I)


def _clean(text):
    text = _ANSI.sub("", str(text))
    return " ".join("".join(c if c.isprintable() or c.isspace() else " "
                            for c in text).split())


def _cells(text):
    return sum(0 if unicodedata.combining(c)
               else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def _take(text, width):
    used = 0
    end = 0
    for end, char in enumerate(text, 1):
        size = _cells(char)
        if used + size > width:
            return text[:end - 1]
        used += size
    return text[:end]


def _truncate(text, width):
    if _cells(text) <= width:
        return text
    if width <= 0:
        return ""
    return _take(text, width - 1).rstrip() + "…"


def _wrap(text, width, limit):
    """Bound the number and terminal-cell width of lines in a display frame."""
    remaining = _clean(text)
    lines = []
    while remaining and len(lines) < limit:
        if _cells(remaining) <= width:
            lines.append(remaining)
            break
        if len(lines) == limit - 1:
            lines.append(_truncate(remaining, width))
            break
        prefix = _take(remaining, width)
        split = prefix.rfind(" ")
        if split > len(prefix) // 3:
            prefix = prefix[:split]
        # A terminal only one cell wide cannot display a two-cell glyph.
        if not prefix:
            lines.append("…")
            remaining = remaining[1:].lstrip()
        else:
            lines.append(prefix.rstrip())
            remaining = remaining[len(prefix):].lstrip()
    return lines


class _DiagnosticStream:
    def __init__(self, console, *, stderr=False):
        self.console = console
        self.stderr = stderr

    @property
    def encoding(self):
        return "utf-8"

    def isatty(self):
        return False

    def writable(self):
        return True

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("diagnostic output must be text")
        self.console._diagnostic_write(text, self.stderr)
        return len(text)

    def flush(self):
        self.console._diagnostic_flush(self.stderr)


class GuidedConsole:
    """Save full diagnostics while making the current instruction prominent.

    Quiet mode is automatic when this helper is used. Critical stdout lines
    and all stderr remain visible. A TTY gets a bounded, replaceable four-line
    display; pipes and verbose mode get one block per cue instead of refreshes.
    All stream, log and renderer updates share a lock. No background thread is
    created here, and the original terminal is never closed.
    """

    def __init__(self, terminal=None, *, verbose=False):
        self.terminal = terminal if terminal is not None else sys.stdout
        self.verbose = verbose
        self.persistent = bool(getattr(self.terminal, "isatty", lambda: False)()) and not verbose
        self._lock = threading.RLock()
        self._log = None
        self._pending = {}
        self._frame = []
        self._frame_key = None
        self._last_cue_key = None

    @contextmanager
    def capture(self, log_path):
        """Route diagnostics to one recording log, restoring streams on exit."""
        with self._lock:
            if self._log is not None:
                raise RuntimeError("GuidedConsole.capture is already active")
            self._log = Path(log_path).open("w", encoding="utf-8", buffering=1)
        stdout = _DiagnosticStream(self)
        stderr = _DiagnosticStream(self, stderr=True)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                yield self
        finally:
            with self._lock:
                for key, text in list(self._pending.items()):
                    if text:
                        self._record_line_locked(text, key[1])
                self._pending.clear()
                self._log.flush()
                self._log.close()
                self._log = None
                # Leave the last visible status on screen. Future calls start
                # a fresh display instead of erasing a completed run's result.
                self._frame = []
                self._frame_key = None
                self._last_cue_key = None

    def _width(self):
        try:
            columns = os.get_terminal_size(self.terminal.fileno()).columns
        except (AttributeError, OSError, ValueError):
            columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        # Avoid automatic terminal wrapping at the final column.
        return max(1, columns - 1)

    def _log_locked(self, text):
        if self._log is not None:
            self._log.write(text)

    def _clear_frame_locked(self):
        for _ in self._frame:
            self.terminal.write("\x1b[1A\r\x1b[2K")
        self._frame = []
        self._frame_key = None

    def _draw_frame_locked(self, lines, key):
        if self._frame_key == key:
            return
        self._clear_frame_locked()
        for line, bold in lines:
            self.terminal.write(("\x1b[1m" + line + "\x1b[0m" if bold else line) + "\n")
        self.terminal.flush()
        self._frame = list(lines)
        self._frame_key = key

    def _visible_line_locked(self, text):
        previous = list(self._frame)
        previous_key = self._frame_key
        self._clear_frame_locked()
        # Preserve diagnostic indentation and spaces inside JSON/string values.
        # Strip terminal control sequences without rewriting the message text.
        safe = _ANSI.sub("", str(text)).rstrip("\n")
        safe = "".join(c for c in safe if c.isprintable() or c == "\t")
        self.terminal.write(safe + "\n")
        if previous:
            self._draw_frame_locked(previous, previous_key)
        self.terminal.flush()

    def _record_line_locked(self, text, stderr):
        self._log_locked(text)
        if self.verbose or stderr or _IMPORTANT.search(text):
            if text.strip():
                self._visible_line_locked(text)

    def _diagnostic_write(self, text, stderr):
        key = (threading.get_ident(), stderr)
        with self._lock:
            pending = self._pending.get(key, "") + text
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                self._record_line_locked(line + "\n", stderr)
            self._pending[key] = pending

    def _diagnostic_flush(self, stderr):
        key = (threading.get_ident(), stderr)
        with self._lock:
            pending = self._pending.get(key, "")
            if pending:
                self._record_line_locked(pending, stderr)
                self._pending[key] = ""
            if self._log is not None:
                self._log.flush()

    def log_prompt(self, line):
        """Save one scheduler event without duplicating the human display."""
        with self._lock:
            self._log_locked(str(line).rstrip("\n") + "\n")

    def status(self, title, detail=""):
        """Replace the current cue with a phase, completion or failure message."""
        title = _clean(title)
        detail = _clean(detail)
        with self._lock:
            self._log_locked(f"[GUIDED] {title}" + (f" — {detail}" if detail else "") + "\n")
            self._last_cue_key = None
            if self.persistent:
                width = self._width()
                lines = [(_truncate(title, width), True)]
                lines.extend((line, False) for line in _wrap(detail, width, 3))
                self._draw_frame_locked(lines, ("status", title, detail, width))
            else:
                self.terminal.write(f"\n*** {title} ***\n")
                if detail:
                    self.terminal.write(detail + "\n")
                self.terminal.flush()

    def show_prompt(self, frame):
        """Display a scheduler frame; refreshes themselves do not enter the log."""
        record = frame["record_index"]
        count = frame["record_count"]
        text = _clean(frame["text"])
        cue_key = (record, count, frame["cue_index"], text)
        remaining = max(0, math.ceil(frame["remaining_s"]))
        next_in = frame["next_in_s"]
        next_in = None if next_in is None else max(0, math.ceil(next_in))
        next_text = _clean(frame.get("next_text") or "")
        with self._lock:
            if self.persistent:
                width = self._width()
                key = (cue_key, remaining, next_in, next_text, width)
                if key == self._frame_key:
                    return
                title = f"GUIDED RECORD {record}/{count}    {remaining}s REMAINING"
                footer = (f"NEXT IN {next_in}s: {next_text}" if next_in is not None
                          else "FINAL INSTRUCTION — wait for the end message")
                lines = [(_truncate(title, width), True)]
                lines.extend((line, True) for line in _wrap("DO NOW: " + text, width, 2))
                lines.append((_truncate(footer, width), False))
                self._draw_frame_locked(lines, key)
            elif cue_key != self._last_cue_key:
                self.terminal.write(
                    f"\n*** RECORD {record}/{count}  +{frame['elapsed_s']:.1f}s — DO NOW ***\n"
                    + text + "\n"
                )
                if next_in is not None:
                    self.terminal.write(f"Next instruction in {next_in}s.\n")
                self.terminal.flush()
            self._last_cue_key = cue_key
