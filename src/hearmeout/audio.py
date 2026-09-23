"""Two-track recording: your microphone (channel 0) and system audio (channel 1).

Keeping the tracks separate gives us "Me" vs "Them" for free, without any
speaker-recognition model. Works on PipeWire (pw-record) and falls back to
plain PulseAudio (parec).
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 16_000  # speech models don't need more


def _commands() -> tuple[list[str], list[str]]:
    fmt = ["--rate", str(RATE), "--channels", "1"]
    if shutil.which("pw-record"):
        base = ["pw-record", *fmt, "--format", "s16"]
        mic = [*base, "-P", "{ node.name=hearmeout-mic node.description=\"Hear Me Out (mic)\" }", "-"]
        system = [
            *base,
            "-P",
            "{ stream.capture.sink=true node.name=hearmeout-system node.description=\"Hear Me Out (system)\" }",
            "-",
        ]
        return mic, system
    if shutil.which("parec"):
        base = ["parec", *fmt, "--format=s16le", "--raw"]
        return [*base, "--device=@DEFAULT_SOURCE@"], [*base, "--device=@DEFAULT_MONITOR@"]
    raise RuntimeError("No audio recorder found: install PipeWire (pw-record) or PulseAudio utilities (parec).")


def backend() -> str:
    return "pipewire" if shutil.which("pw-record") else "pulseaudio" if shutil.which("parec") else "none"


class Recorder:
    """Records both tracks to raw files until stop() is called."""

    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._procs: list[subprocess.Popen] = []
        self._threads: list[threading.Thread] = []
        self.started_at: float | None = None

    def start(self) -> None:
        for name, cmd in zip(("mic", "system"), _commands()):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)  # Ctrl+C stops us, not the recorders
            out = open(self.dir / f"{name}.pcm", "wb")
            t = threading.Thread(target=_pump, args=(proc, out), daemon=True)
            t.start()
            self._procs.append(proc)
            self._threads.append(t)
        self.started_at = time.time()

    def stop(self) -> Path:
        """Stop recording and return the path of the combined stereo Opus file."""
        for p in self._procs:
            p.send_signal(signal.SIGINT)
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for t in self._threads:
            t.join()
        return combine(self.dir / "mic.pcm", self.dir / "system.pcm", self.dir / "audio.ogg")


def _pump(proc: subprocess.Popen, out) -> None:
    with out:
        while chunk := proc.stdout.read(8192):
            out.write(chunk)


def _load_pcm(path: Path) -> np.ndarray:
    data = path.read_bytes()
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2")


def combine(mic: Path, system: Path, dest: Path) -> Path:
    """Interleave the two mono tracks into one stereo file, then delete the raw tracks."""
    a, b = _load_pcm(mic), _load_pcm(system)
    n = max(len(a), len(b))
    stereo = np.zeros((n, 2), dtype="<i2")
    stereo[: len(a), 0] = a
    stereo[: len(b), 1] = b
    sf.write(dest, stereo, RATE, format="OGG", subtype="OPUS")
    mic.unlink()
    system.unlink()
    return dest
