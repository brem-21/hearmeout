"""Two-track recording: your microphone (channel 0) and system audio (channel 1).

Keeping the tracks separate gives us "Me" vs "Them" for free, without any
speaker-recognition model. Works on PipeWire (pw-record) and falls back to
plain PulseAudio (parec).

By default the tracks follow the system's default mic and speaker. During a
detected call, follow() moves them onto the devices the call app is really
using, e.g. a USB or Bluetooth headset, even if it's switched mid-meeting.
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
# Voice detection, for "stop after N minutes of silence" and the level meters.
VOICE_MIN_RMS = 150     # quietest level we count as someone talking (16-bit samples)
VOICE_OVER_FLOOR = 3.0  # …and it must be this many times louder than the background noise


def default_devices() -> tuple[str | None, str | None]:
    """Names of the system's default mic and speaker."""
    try:
        import pulsectl
        with pulsectl.Pulse("hearmeout-defaults") as p:
            info = p.server_info()
            return info.default_source_name, info.default_sink_name
    except Exception:
        return None, None


def _commands(mic: str | None, speaker: str | None) -> tuple[list[str], list[str]]:
    fmt = ["--rate", str(RATE), "--channels", "1"]
    if shutil.which("pw-record"):
        # Our own media role, so the session manager's memory of where our streams went
        # never mixes with other apps' streams; explicit targets so nothing stale is reused.
        base = ["pw-record", *fmt, "--format", "s16", "--media-category", "Capture", "--media-role", "Notes"]
        mic_cmd = [*base, *(["--target", mic] if mic else []),
                   "-P", "{ node.name=hearmeout-mic node.description=\"Hear Me Out (mic)\" }", "-"]
        system_cmd = [*base, *(["--target", speaker] if speaker else []),
                      "-P", "{ stream.capture.sink=true node.name=hearmeout-system "
                            "node.description=\"Hear Me Out (system)\" }", "-"]
        return mic_cmd, system_cmd
    if shutil.which("parec"):
        base = ["parec", *fmt, "--format=s16le", "--raw"]
        return ([*base, "--client-name=hearmeout-mic", f"--device={mic or '@DEFAULT_SOURCE@'}"],
                [*base, "--client-name=hearmeout-system",
                 f"--device={speaker + '.monitor' if speaker else '@DEFAULT_MONITOR@'}"])
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
        self._pulse = None
        self.devices: tuple[str | None, str | None] = (None, None)  # (mic, speaker) we moved to
        self.levels = {"mic": 0.0, "system": 0.0}      # 0..1, for level meters
        self.last_voice = time.time()                  # when anyone last spoke, either side
        self._floor = {"mic": None, "system": None}    # background-noise estimate per track

    def start(self, mic: str | None = None, speaker: str | None = None) -> None:
        """Start recording, from the given devices or else the current defaults."""
        if not (mic and speaker):
            dmic, dspeaker = default_devices()
            mic, speaker = mic or dmic, speaker or dspeaker
        self.devices = (mic, speaker)
        for name, cmd in zip(("mic", "system"), _commands(mic, speaker)):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)  # Ctrl+C stops us, not the recorders
            out = open(self.dir / f"{name}.pcm", "wb")
            t = threading.Thread(target=self._pump, args=(name, proc, out), daemon=True)
            t.start()
            self._procs.append(proc)
            self._threads.append(t)
        self.started_at = self.last_voice = time.time()

    def silent_for(self) -> float:
        """Seconds since anyone (you or the others) last spoke."""
        return time.time() - self.last_voice

    def _pump(self, track: str, proc: subprocess.Popen, out) -> None:
        """Copy the recorder's audio to disk, measuring its loudness on the way."""
        with out:
            while chunk := proc.stdout.read(8192):  # about a quarter of a second
                out.write(chunk)
                samples = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype="<i2").astype(np.float32)
                if not len(samples):
                    continue
                rms = float(np.sqrt(np.mean(samples * samples)))
                floor = self._floor[track]
                # Background noise: follows quiet moments quickly, loud ones only very slowly.
                floor = rms if floor is None or rms < floor else floor + (rms - floor) * 0.005
                self._floor[track] = floor
                if rms > max(VOICE_MIN_RMS, floor * VOICE_OVER_FLOOR):
                    self.last_voice = time.time()
                db = 20 * np.log10(max(rms, 1.0) / 32768)
                self.levels[track] = float(min(1.0, max(0.0, (db + 60) / 60)))

    def follow(self, mic: str | None, speaker: str | None) -> bool:
        """Record from this mic and from what's playing on this speaker (by device name).
        Returns True if a track was moved."""
        if (mic, speaker) == self.devices or not (mic or speaker):
            return False
        try:
            import pulsectl
            if self._pulse is None:
                self._pulse = pulsectl.Pulse("hearmeout-recorder")
            p = self._pulse
            ours = {}
            for so in p.source_output_list():
                name = so.proplist.get("node.name") or so.proplist.get("application.name") or ""
                for track in ("mic", "system"):
                    if name.startswith(f"hearmeout-{track}"):
                        ours[track] = so
            sources = {s.name: s for s in p.source_list()}
            monitors = {s.name: s.monitor_source_name for s in p.sink_list()}
            moved = False
            for track, target in (("mic", mic), ("system", monitors.get(speaker) if speaker else None)):
                so, src = ours.get(track), sources.get(target) if target else None
                if so is not None and src is not None and so.source != src.index:
                    p.source_output_move(so.index, src.index)
                    moved = True
            if len(ours) == 2:  # both streams exist: remember, so we don't redo this every poll
                self.devices = (mic, speaker)
            return moved
        except Exception:  # the sound server went away, a device vanished…: keep recording as is
            return False

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
        if self._pulse is not None:
            self._pulse.close()
            self._pulse = None
        return combine(self.dir / "mic.pcm", self.dir / "system.pcm", self.dir / "audio.ogg")


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
