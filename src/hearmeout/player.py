"""Play a recording from any point, through the sound server's own player
(pw-play on PipeWire, paplay on PulseAudio). Mixed to mono, so you hear both
sides of the call in both ears."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from PySide6.QtCore import QObject, QTimer, Signal


def _command(rate: int) -> list[str] | None:
    if shutil.which("pw-play"):
        return ["pw-play", "--rate", str(rate), "--channels", "1", "--format", "s16", "-"]
    if shutil.which("paplay"):
        return ["paplay", "--raw", f"--rate={rate}", "--channels=1", "--format=s16le"]
    if shutil.which("aplay"):
        return ["aplay", "-q", "-f", "S16_LE", "-r", str(rate), "-c", "1", "-"]
    return None


class Player(QObject):
    position = Signal(float)   # seconds, about 4 times a second while playing
    state = Signal(bool)       # True when playing starts, False when it stops

    def __init__(self):
        super().__init__()
        self.path: Path | None = None
        self.offset = 0.0      # where the current play started
        self._t0 = 0.0
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.duration = 0.0

    @staticmethod
    def available() -> bool:
        return _command(16000) is not None

    @property
    def playing(self) -> bool:
        return self._proc is not None

    def now(self) -> float:
        return self.offset + (time.monotonic() - self._t0 if self.playing else 0.0)

    def play(self, path: Path, start: float = 0.0) -> None:
        self.stop(emit=False)
        info = sf.info(path)
        cmd = _command(info.samplerate)
        if cmd is None:
            return
        self.path, self.duration = path, info.duration
        self.offset = max(0.0, min(start, info.duration))
        self._stop.clear()
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._feed, args=(path, self._proc, int(self.offset * info.samplerate)),
                         daemon=True).start()
        self._t0 = time.monotonic()
        self._timer.start(250)
        self.state.emit(True)

    def _feed(self, path: Path, proc: subprocess.Popen, frame: int) -> None:
        try:
            with sf.SoundFile(path) as f:
                f.seek(frame)
                for block in f.blocks(blocksize=f.samplerate // 5, dtype="int16", always_2d=True):
                    if self._stop.is_set():
                        break
                    mono = block.astype(np.int32).mean(axis=1).astype("<i2")
                    proc.stdin.write(mono.tobytes())
            proc.stdin.close()
            proc.wait()
        except (OSError, ValueError, BrokenPipeError):
            pass

    def _tick(self) -> None:
        if self._proc is not None and self._proc.poll() is not None:
            self.stop()
            return
        self.position.emit(self.now())

    def pause(self) -> None:
        """Stop, remembering where; play(path, player.offset) resumes."""
        pos = self.now()
        self.stop()
        self.offset = pos

    def stop(self, emit: bool = True) -> None:
        self._timer.stop()
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None
            if emit:
                self.state.emit(False)
