"""Optional time-aligned Matrix32 command/ACK event log.

Each JSONL event carries the same PC monotonic clock used by MCU CSV arrival
timestamps. Host BLE write/ACK timestamps are observations, not electrical
waveform or device-application timestamps. Files are created only by --record.
"""
import json
import time
from datetime import datetime
from pathlib import Path


class Matrix32Trace:
    def __init__(self, directory, recording_id):
        self.path = Path(directory) / f"{recording_id}_matrix32_commands.jsonl"
        self._file = self.path.open("x", encoding="utf-8")

    def emit(self, event, **details):
        if self._file is None:
            return
        row = {
            "event": event,
            "pc_monotonic_ns": time.monotonic_ns(),
            "pc_wall_iso": datetime.now().astimezone().isoformat(),
            **details,
        }
        self._file.write(json.dumps(row, default=str, separators=(",", ":")) + "\n")
        self._file.flush()

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


class OptionalCapture:
    """Shared session handle; no files or writes unless recording is enabled."""

    def __init__(self, enabled, directory, config):
        self.enabled = enabled
        self.directory = directory
        self.config = config
        self.recorder = None
        self.trace = None

    def start(self, applied_settings):
        if not self.enabled:
            return
        from recording.data_recorder import DataRecorder
        self.recorder = DataRecorder(self.directory)
        self.recorder.start(config=self.config, applied_settings=applied_settings)
        self.trace = Matrix32Trace(self.directory, self.recorder.recording_id)
        self.emit("recording_started", recording_id=self.recorder.recording_id)
        print(f"[RECORD] MCU and Matrix32 trace: {self.recorder.recording_id}")

    def emit(self, event, **details):
        if self.trace is not None:
            self.trace.emit(event, **details)

    def close(self):
        try:
            if self.recorder is not None:
                self.recorder.stop()
        finally:
            if self.trace is not None:
                self.emit("recording_closed")
                self.trace.close()
                self.trace = None
