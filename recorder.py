"""Small Windows GUI for recording system playback audio with WASAPI loopback."""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _init_com(apartment_threaded: bool) -> bool:
    """Initialize COM on the calling thread. Returns True if it must be released."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2 if apartment_threaded else 0x0)
        return hr in (0, 1)  # S_OK or S_FALSE: balanced by CoUninitialize
    except Exception:
        return False


def _release_com(initialized: bool) -> None:
    if initialized:
        try:
            import ctypes

            ctypes.windll.ole32.CoUninitialize()
        except Exception:
            pass


# Tk's native folder dialog needs a single-threaded apartment on the GUI thread.
# SoundCard would otherwise put this thread in the multithreaded apartment when
# it is imported; it accepts an existing apartment. Capture threads join the
# multithreaded apartment themselves (see CaptureEngine._run).
_init_com(apartment_threaded=True)

import numpy as np  # noqa: E402
import soundcard as sc  # noqa: E402
try:  # PortAudio is only needed for the optional microphone.
    import sounddevice as sd  # noqa: E402
    SOUNDDEVICE_ERROR: str | None = None
except Exception as _sounddevice_error:  # noqa: BLE001
    sd = None  # type: ignore[assignment]
    SOUNDDEVICE_ERROR = str(_sounddevice_error)
import soundfile as sf  # noqa: E402
import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402


APP_TITLE = "Meeting System Audio Recorder"
APP_ID = "MeetingSystemAudioRecorder"
SAMPLE_RATE = 48_000
BLOCK_SIZE = 4_800  # 100 ms at 48 kHz
ENVELOPE_POINTS_PER_BLOCK = 10  # one waveform column per 10 ms
LOOPBACK_BUFFER_FRAMES = 24_000  # 500 ms WASAPI buffer: tolerate short stalls without loss
MICROPHONE_BUFFER_SECONDS = 0.15  # drift/latency cap for the microphone mix
# WAV stores sizes in 32 bits. Start a new part well before the 4 GiB limit
# (about 6 hours of 48 kHz stereo 16-bit audio).
MAX_WAV_DATA_BYTES = 0xFFFFFFFF - 64 * 1024 * 1024


def application_dir() -> Path:
    """Folder containing the script, or the packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_ID / "settings.json"


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    """Write settings atomically, so an interrupted save never corrupts them."""
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(settings, stream, indent=2)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except OSError:
        pass  # Settings are a convenience; never block recording on them.


def directory_is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        handle, probe = tempfile.mkstemp(dir=path, prefix=".write-test-")
        os.close(handle)
        os.unlink(probe)
        return True
    except OSError:
        return False


def default_output_dir() -> Path:
    """Next to the app when possible, otherwise in the user's Music folder."""
    beside_app = application_dir() / "recordings"
    if directory_is_writable(beside_app):
        return beside_app
    return Path.home() / "Music" / "Meeting Recordings"


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioDevice:
    name: str
    device_id: str
    channels: int  # channels captured and saved
    wasapi: bool = False
    native_channels: int = 0  # what the device reports, for diagnostics


# Backwards-compatible name used by earlier versions.
PlaybackDevice = AudioDevice


def available_playback_devices() -> list[AudioDevice]:
    devices: list[AudioDevice] = []
    for speaker in sc.all_speakers():
        try:  # one endpoint vanishing mid-scan must not hide the others
            native = int(getattr(speaker, "channels", 0) or 0)
            devices.append(
                AudioDevice(
                    name=str(speaker.name),
                    device_id=str(speaker.id),
                    # SoundCard 0.4.x documents corrupt single-channel WASAPI
                    # capture, so always capture (and save) stereo.
                    channels=2,
                    wasapi=True,
                    native_channels=native,
                )
            )
        except Exception:
            continue
    return devices


def _require_sounddevice() -> Any:
    if sd is None:
        raise RuntimeError(f"Microphone support is unavailable: {SOUNDDEVICE_ERROR}")
    return sd


def reload_portaudio() -> None:
    """Make PortAudio rescan devices. Only call while no stream is open."""
    backend = _require_sounddevice()
    try:
        backend._terminate()  # noqa: SLF001 - sounddevice has no public rescan API
    finally:
        backend._initialize()  # noqa: SLF001


def _wasapi_hostapi_index() -> int | None:
    for index, host_api in enumerate(_require_sounddevice().query_hostapis()):
        if "WASAPI" in str(host_api["name"]).upper():
            return index
    return None


def available_microphones() -> list[AudioDevice]:
    backend = _require_sounddevice()
    device_info = list(backend.query_devices())
    wasapi_index = _wasapi_hostapi_index()
    devices: list[AudioDevice] = []
    for index, info in enumerate(device_info):
        if int(info["max_input_channels"]) <= 0:
            continue
        host_index = int(info["hostapi"])
        if wasapi_index is not None and host_index != wasapi_index:
            continue
        devices.append(
            AudioDevice(
                name=str(info["name"]),
                device_id=str(index),
                channels=int(info["max_input_channels"]),
                wasapi=host_index == wasapi_index,
                native_channels=int(info["max_input_channels"]),
            )
        )
    return devices


def default_device_id() -> str | None:
    try:
        speaker = sc.default_speaker()
        return str(speaker.id) if speaker else None
    except Exception:
        return None


def default_microphone_id() -> str | None:
    try:
        backend = _require_sounddevice()
        wasapi_index = _wasapi_hostapi_index()
        if wasapi_index is not None:
            index = int(backend.query_hostapis(wasapi_index)["default_input_device"])
        else:
            index = int(backend.default.device[0])
        return str(index) if index >= 0 else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def safe_filename_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("meeting_%Y-%m-%d_%H-%M-%S")


def _stem_in_use(output_dir: Path, stem: str) -> bool:
    return (
        (output_dir / f"{stem}.wav").exists()
        or (output_dir / f"{stem}.recording.wav").exists()
        or any(output_dir.glob(f"{stem}_part*.wav"))
    )


def unused_recording_stem(output_dir: Path, now: datetime | None = None) -> str:
    stem = safe_filename_timestamp(now)
    suffix = 1
    while True:
        candidate = stem if suffix == 1 else f"{stem}_{suffix}"
        if not _stem_in_use(output_dir, candidate):
            return candidate
        suffix += 1


def unused_recording_paths(output_dir: Path) -> tuple[Path, Path]:
    """Return final/partial paths without overwriting any earlier recording."""
    stem = unused_recording_stem(output_dir)
    return output_dir / f"{stem}.wav", output_dir / f"{stem}.recording.wav"


SFC_SET_UPDATE_HEADER_AUTO = 0x1061


def _reserve(path: Path) -> bool:
    """Create ``path`` only if it does not exist yet (safe across processes)."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


class RecordingFile:
    """A WAV file written under a temporary name and renamed when complete."""

    def __init__(self, output_dir: Path, stem: str, part: int, channels: int) -> None:
        self.output_dir = output_dir
        self.part = part
        self.channels = channels
        self.frames = 0
        self.rename_error: OSError | None = None
        suffix = 1
        while True:
            session = stem if suffix == 1 else f"{stem}_{suffix}"
            name = session if part == 1 else f"{session}_part{part}"
            self.final_path = output_dir / f"{name}.wav"
            self.partial_path = output_dir / f"{name}.recording.wav"
            # Reserve the temporary name atomically so two app instances that
            # start in the same second never share a file.
            if not self.final_path.exists() and _reserve(self.partial_path):
                break
            suffix += 1
        # Later parts keep the session name actually reserved for part 1.
        self.stem = session if part == 1 else stem
        try:
            self._file = sf.SoundFile(
                self.partial_path,
                mode="w",
                samplerate=SAMPLE_RATE,
                channels=channels,
                subtype="PCM_16",
                format="WAV",
            )
        except Exception:
            self._remove_partial()
            raise
        # Rewrite the WAV header after every write, so the temporary file stays
        # playable up to the last completed write if the app stops unexpectedly.
        try:
            sf._snd.sf_command(  # noqa: SLF001 - not exposed by SoundFile
                self._file._file, SFC_SET_UPDATE_HEADER_AUTO, sf._ffi.NULL, 1  # noqa: SLF001
            )
        except Exception:
            pass

    @property
    def data_bytes(self) -> int:
        return self.frames * self.channels * 2

    def write(self, data: np.ndarray) -> None:
        self._file.write(data)
        self.frames += len(data)

    def next_part(self) -> RecordingFile:
        return RecordingFile(self.output_dir, self.stem, self.part + 1, self.channels)

    def _remove_partial(self) -> None:
        try:
            self.partial_path.unlink()
        except OSError:
            pass

    def discard(self) -> None:
        """Close and delete this writer's own temporary file (never published)."""
        try:
            if not self._file.closed:
                self._file.close()
        finally:
            self._remove_partial()

    def finalize(self) -> Path:
        """Close the file and give it its final name. Returns where it is.

        If renaming fails, the audio stays in the temporary file, which is
        returned, and ``rename_error`` holds the reason.
        """
        if not self._file.closed:
            self._file.close()
        target = self.final_path
        for attempt in range(2, 100):
            try:
                # os.rename never overwrites an existing file on Windows.
                os.rename(self.partial_path, target)
                self.final_path = target
                return target
            except FileExistsError:
                target = target.with_name(f"{self.final_path.stem}_{attempt}.wav")
            except OSError as exc:
                self.rename_error = exc
                break
        else:
            self.rename_error = FileExistsError(f"Every name like {self.final_path.name} is taken.")
        return self.partial_path


def _is_empty_wav(path: Path) -> bool:
    """Remove and report WAV files that contain no audio frames."""
    try:
        if sf.info(str(path)).frames == 0:
            path.unlink()
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# Audio processing helpers
# --------------------------------------------------------------------------


def envelope(data: np.ndarray, points: int = ENVELOPE_POINTS_PER_BLOCK) -> np.ndarray:
    """Peak level of consecutive slices of a block, for the scrolling scope."""
    if data.size == 0:
        return np.zeros(0, dtype=np.float32)
    magnitude = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
    slices = np.array_split(magnitude, min(points, len(magnitude)))
    return np.array([float(s.max()) if s.size else 0.0 for s in slices], dtype=np.float32)


def peak(data: np.ndarray | None) -> float:
    return float(np.max(np.abs(data))) if data is not None and data.size else 0.0


def mix_sources(system: np.ndarray, microphone: np.ndarray | None) -> np.ndarray:
    """Mix a mono microphone into the system channels and keep it in range."""
    system = np.asarray(system, dtype=np.float32)
    if system.ndim == 1:
        system = system[:, np.newaxis]
    if microphone is None:
        mixed = system.copy()
    else:
        # Halving both sources leaves headroom and prevents hard clipping.
        mixed = 0.5 * system + 0.5 * np.asarray(microphone, dtype=np.float32)[:, np.newaxis]
    # Float loopback audio can exceed full scale; PCM conversion must not wrap.
    np.clip(mixed, -1.0, 1.0, out=mixed)
    return mixed


class MicrophoneBuffer:
    """Thread-safe FIFO between the microphone callback and the mixer.

    The microphone and the playback device run on separate clocks. The mixer
    takes exactly as many frames as the system block has: missing frames are
    filled with silence, and the buffer never holds more than ``max_frames``,
    so a faster microphone clock cannot build up delay. ``clear`` drops audio
    that arrived while the devices were still starting.
    """

    def __init__(self, max_frames: int) -> None:
        self.max_frames = max_frames
        self._lock = threading.Lock()
        self._chunks: deque[np.ndarray] = deque()
        self._frames = 0
        self.last_push = time.monotonic()
        self.dropped_frames = 0
        self.padded_frames = 0

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._frames = 0

    def push(self, data: np.ndarray) -> None:
        data = np.asarray(data, dtype=np.float32)
        mono = data.mean(axis=1, dtype=np.float32) if data.ndim > 1 else data.copy()
        with self._lock:
            self._chunks.append(mono)
            self._frames += len(mono)
            excess = self._frames - self.max_frames
            while excess > 0 and self._chunks:
                head = self._chunks[0]
                if len(head) <= excess:
                    self._chunks.popleft()
                    self._frames -= len(head)
                    self.dropped_frames += len(head)
                    excess -= len(head)
                else:
                    self._chunks[0] = head[excess:]
                    self._frames -= excess
                    self.dropped_frames += excess
                    excess = 0
            self.last_push = time.monotonic()

    def take(self, count: int) -> np.ndarray:
        out = np.zeros(count, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < count and self._chunks:
                head = self._chunks[0]
                used = min(len(head), count - filled)
                out[filled : filled + used] = head[:used]
                if used == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[used:]
                self._frames -= used
                filled += used
            self.padded_frames += count - filled
        return out


def open_microphone_stream(microphone: AudioDevice, buffer: MicrophoneBuffer) -> Any:
    backend = _require_sounddevice()

    def callback(indata: np.ndarray, _frames: int, _time: Any, _status: Any) -> None:
        buffer.push(indata)

    # Shared-mode WASAPI only accepts the device's own mix format unless
    # PortAudio is allowed to convert sample rate and channel count.
    extra = backend.WasapiSettings(auto_convert=True) if microphone.wasapi else None
    return backend.InputStream(
        device=int(microphone.device_id),
        samplerate=SAMPLE_RATE,
        channels=max(1, min(microphone.channels, 2)),
        dtype="float32",
        callback=callback,
        extra_settings=extra,
    )


def _microphone_healthy(stream: Any, buffer: MicrophoneBuffer) -> bool:
    try:
        active = bool(stream.active)
    except Exception:
        return False
    return active and time.monotonic() - buffer.last_push < 2.0


# --------------------------------------------------------------------------
# Capture engine
# --------------------------------------------------------------------------


class CaptureEngine:
    """Runs one capture thread that previews audio and, on request, records it.

    Starting a recording attaches a WAV file to the running capture, so the
    devices are not reopened. Start and Stop take effect at the next 100 ms
    block. Every event is ``(name, payload, generation)``; ``generation`` grows
    with each ``start`` so the UI can ignore events from a replaced capture.

    ``capturing``        payload: has_microphone
    ``level``            payload: (system_peak, mic_peak, envelope, recorded_seconds | None)
    ``recording``        payload: path; the first block was written
    ``saved``            payload: (paths, seconds, notes); the recording finished
    ``microphone``       payload: True/False when the microphone recovers/stops
    ``error``            payload: (exc, paths, seconds); capture failed
    ``capture_stopped``  payload: None; the capture thread has exited
    """

    def __init__(self, events: queue.Queue[tuple[str, Any, int]]) -> None:
        self.events = events
        self.generation = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._device: AudioDevice | None = None
        self._accepting = False
        self._pending: RecordingFile | None = None
        self._recording = False
        self._end_requested = False
        self.microphone_ok = True

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording or self._pending is not None

    def start(self, device: AudioDevice, microphone: AudioDevice | None = None) -> int:
        if self.running:
            raise RuntimeError("Audio capture is already running.")
        self.generation += 1
        self._stop = threading.Event()  # a fresh event per run: old threads keep theirs
        self._device = device
        self.microphone_ok = True
        with self._lock:
            self._accepting = True
            self._pending = None
            self._recording = False
            self._end_requested = False
        self._thread = threading.Thread(
            target=self._run,
            args=(device, microphone, self.generation, self._stop),
            daemon=True,
            name=f"audio-capture-{self.generation}",
        )
        self._thread.start()
        return self.generation

    def request_stop(self) -> None:
        with self._lock:
            self._accepting = False  # a stopping capture must not take a new recording
        self._stop.set()

    def stop_and_wait(self, timeout: float = 5.0) -> bool:
        """Stop capturing (finalizing any recording). True if the thread ended."""
        self.request_stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        return not self.running

    def begin_recording(self, output_dir: Path) -> Path:
        if self._device is None or not self.running:
            raise RuntimeError("Audio capture is not running for the selected device.")
        with self._lock:
            if self._recording or self._pending is not None:
                raise RuntimeError("A recording is already in progress.")
        output_dir.mkdir(parents=True, exist_ok=True)
        writer = RecordingFile(output_dir, unused_recording_stem(output_dir), 1, self._device.channels)
        with self._lock:
            if self._accepting:
                self._pending = writer
                self._end_requested = False
                return writer.final_path
        # The capture thread ended in the meantime: remove only our own empty file.
        writer.discard()
        raise RuntimeError("Audio capture stopped before the recording could start.")

    def end_recording(self) -> None:
        with self._lock:
            self._end_requested = True

    def _run(
        self,
        device: AudioDevice,
        microphone: AudioDevice | None,
        generation: int,
        stop: threading.Event,
    ) -> None:
        com = _init_com(apartment_threaded=False)
        try:
            self._capture(device, microphone, generation, stop)
        finally:
            _release_com(com)

    def _capture(
        self,
        device: AudioDevice,
        microphone: AudioDevice | None,
        generation: int,
        stop: threading.Event,
    ) -> None:
        def post(event: str, payload: Any = None) -> None:
            self.events.put((event, payload, generation))

        writer: RecordingFile | None = None
        saved: list[Path] = []
        notes: list[str] = []
        next_part_of: RecordingFile | None = None  # rollover opens the next part lazily
        recording_active = False
        recorded_frames = 0
        error: BaseException | None = None
        microphone_stream: Any = None
        try:
            loopback = sc.get_microphone(device.device_id, include_loopback=True)
            if loopback is None:
                raise RuntimeError("Windows could not open loopback capture for this device.")
            microphone_buffer = (
                MicrophoneBuffer(int(SAMPLE_RATE * MICROPHONE_BUFFER_SECONDS))
                if microphone is not None
                else None
            )
            with loopback.recorder(
                samplerate=SAMPLE_RATE,
                channels=device.channels,
                blocksize=LOOPBACK_BUFFER_FRAMES,
            ) as system_recorder:
                if microphone is not None and microphone_buffer is not None:
                    microphone_stream = open_microphone_stream(microphone, microphone_buffer)
                    microphone_stream.start()
                post("capturing", microphone is not None)
                first_block = True
                while not stop.is_set():
                    system_data = system_recorder.record(numframes=BLOCK_SIZE)
                    microphone_data = None
                    if microphone_buffer is not None:
                        if first_block:
                            # Drop microphone audio captured while loopback was
                            # still starting, so the two sources line up.
                            microphone_buffer.clear()
                        microphone_data = microphone_buffer.take(len(system_data))
                        healthy = _microphone_healthy(microphone_stream, microphone_buffer)
                        if healthy != self.microphone_ok:
                            self.microphone_ok = healthy
                            post("microphone", healthy)
                    first_block = False
                    mixed = mix_sources(system_data, microphone_data)

                    with self._lock:
                        if self._pending is not None and not recording_active:
                            writer = self._pending
                            self._pending = None
                            self._recording = True
                            recording_active = True
                            saved, notes, recorded_frames = [], [], 0
                            just_started = True
                        else:
                            just_started = False
                        end_now = self._end_requested
                        self._end_requested = False

                    if recording_active:
                        if writer is None and next_part_of is not None:
                            writer = next_part_of.next_part()
                            next_part_of = None
                        assert writer is not None
                        writer.write(mixed)
                        recorded_frames += len(mixed)
                        if just_started:
                            post("recording", writer.final_path)
                        if writer.data_bytes >= MAX_WAV_DATA_BYTES or end_now:
                            finished, writer = writer, None
                            try:
                                saved.append(finished.finalize())
                            except Exception:
                                saved.append(finished.partial_path)  # keep it recoverable
                                raise
                            if finished.rename_error is not None:
                                notes.append(f"Could not rename the file: {finished.rename_error}")
                            if not end_now:
                                next_part_of = finished
                        if end_now:
                            recording_active = False
                            next_part_of = None
                            with self._lock:
                                self._recording = False
                            post("saved", (saved, recorded_frames / SAMPLE_RATE, notes))
                            saved, notes = [], []

                    post(
                        "level",
                        (
                            peak(np.asarray(system_data)),
                            peak(microphone_data) if microphone_data is not None else None,
                            envelope(mixed),
                            recorded_frames / SAMPLE_RATE if recording_active else None,
                        ),
                    )
        except BaseException as exc:  # noqa: BLE001 - reported to the UI
            error = exc
        finally:
            # Every step is best effort: the UI must always get a final event.
            try:
                if microphone_stream is not None:
                    try:
                        microphone_stream.close()
                    except Exception:
                        pass
                with self._lock:
                    self._accepting = False
                    pending, self._pending = self._pending, None
                    self._end_requested = False
                if pending is not None:
                    pending.discard()  # attached after the last block: no audio
                if writer is not None:
                    try:
                        saved.append(writer.finalize())
                        if writer.rename_error is not None:
                            notes.append(f"Could not rename the file: {writer.rename_error}")
                    except Exception as finalize_error:  # noqa: BLE001
                        error = error or finalize_error
                        saved.append(writer.partial_path)
                saved = [path for path in saved if not _is_empty_wav(path)]
            except BaseException as cleanup_error:  # noqa: BLE001
                error = error or cleanup_error
            finally:
                with self._lock:
                    self._recording = False
                seconds = recorded_frames / SAMPLE_RATE
                if error is not None:
                    post("error", (error, saved, seconds))
                elif recording_active:
                    post("saved", (saved, seconds, notes))
                post("capture_stopped")


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------

COLORS = {
    "bg": "#f3f4f6",
    "card": "#ffffff",
    "border": "#e5e7eb",
    "text": "#111827",
    "muted": "#6b7280",
    "subtle": "#9ca3af",
    "accent": "#2563eb",
    "rec": "#dc2626",
    "rec_hover": "#b91c1c",
    "ok": "#16a34a",
    "warn": "#d97706",
    "idle_dot": "#9ca3af",
    "scope_bg": "#0f172a",
    "scope_axis": "#1e293b",
    "scope_quiet": "#475569",
    "scope_live": "#38bdf8",
    "scope_rec": "#f87171",
    "meter_bg": "#eef0f3",
}
UI_FONT = "Segoe UI"
MICROPHONE_LOST_NOTICE = (
    "⚠  The microphone stopped sending audio (unplugged or muted by Windows?). "
    "System audio is still being captured."
)


def enable_dpi_awareness() -> None:
    """Render crisply on high-DPI Windows displays instead of bitmap-scaled."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def level_to_fraction(level: float) -> float:
    """Map a 0..1 peak to a -60..0 dBFS meter position."""
    if level <= 1e-6:
        return 0.0
    db = 20.0 * np.log10(level)
    return float(min(1.0, max(0.0, (db + 60.0) / 60.0)))


def reveal_in_explorer(path: Path) -> None:
    try:
        if path.is_file():
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception as exc:
        messagebox.showerror(APP_TITLE, f"Could not open the folder:\n\n{exc}")


class LevelMeter(tk.Canvas):
    """A thin horizontal peak meter with a decaying hold."""

    def __init__(self, master: tk.Misc, width: int, height: int) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            background=COLORS["card"],
            highlightthickness=0,
        )
        self._level = 0.0
        self._enabled = True
        self.bind("<Configure>", lambda _e: self._draw())

    def set_level(self, level: float | None, immediate: bool = False) -> None:
        target = 0.0 if level is None else level_to_fraction(level)
        if immediate or target > self._level:
            self._level = target
        else:  # fast attack, slow release, like a hardware meter
            self._level = self._level * 0.75 + target * 0.25
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self._level = 0.0
        self._draw()

    def _draw(self) -> None:
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height())
        self.delete("all")
        self.create_rectangle(0, 0, width, height, fill=COLORS["meter_bg"], outline="")
        if not self._enabled:
            return
        fill = int(width * self._level)
        if fill <= 0:
            return
        if self._level > 0.95:
            color = COLORS["rec"]
        elif self._level > 0.8:
            color = COLORS["warn"]
        else:
            color = COLORS["ok"]
        self.create_rectangle(0, 0, fill, height, fill=color, outline="")


class RecordButton(tk.Canvas):
    """Round record/stop button: a red dot that becomes a stop square."""

    def __init__(self, master: tk.Misc, size: int, command: Callable[[], None]) -> None:
        super().__init__(
            master,
            width=size,
            height=size,
            background=COLORS["card"],
            highlightthickness=0,
            takefocus=1,
            cursor="hand2",
        )
        self.size = size
        self.command = command
        self.recording = False
        self.enabled = True
        self.hover = False
        self.focused = False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._on_click)
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self._key_held = False
        for key in ("space", "Return"):
            self.bind(f"<KeyPress-{key}>", self._on_key)
            self.bind(f"<KeyRelease-{key}>", self._on_key_release)
        self._draw()

    def _on_key(self, _event: tk.Event) -> str:
        if not self._key_held:  # ignore auto-repeat while the key is held
            self._key_held = True
            self._invoke()
        return "break"

    def _on_key_release(self, _event: tk.Event) -> None:
        self._key_held = False

    def configure_state(self, recording: bool, enabled: bool) -> None:
        self.recording = recording
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow", takefocus=1 if enabled else 0)
        self._draw()

    def _set_hover(self, hover: bool) -> None:
        self.hover = hover
        self._draw()

    def _set_focus(self, focused: bool) -> None:
        self.focused = focused
        self._draw()

    def _on_click(self, event: tk.Event) -> None:
        if 0 <= event.x <= self.size and 0 <= event.y <= self.size:
            self._invoke()

    def _invoke(self) -> None:
        if self.enabled:
            self.command()

    def _draw(self) -> None:
        self.delete("all")
        s = self.size
        ring = max(2, s // 18)
        if not self.enabled:
            fill = "#e5e7eb"
            ring_color = "#d1d5db"
        elif self.hover:
            fill = COLORS["rec_hover"]
            ring_color = "#fecaca"
        else:
            fill = COLORS["rec"]
            ring_color = "#fee2e2"
        if self.focused and self.enabled:
            ring_color = "#93c5fd"
        self.create_oval(1, 1, s - 1, s - 1, fill=ring_color, outline="")
        inset = ring + 3
        self.create_oval(inset, inset, s - inset, s - inset, fill=fill, outline="")
        glyph = "#ffffff" if self.enabled else "#9ca3af"
        if self.recording:
            half = s * 0.14
            c = s / 2
            self.create_rectangle(c - half, c - half, c + half, c + half, fill=glyph, outline="")
        else:
            radius = s * 0.13
            c = s / 2
            self.create_oval(c - radius, c - radius, c + radius, c + radius, fill=glyph, outline="")


class RecorderApp:
    SCOPE_SECONDS = 4.0

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.root.title(APP_TITLE)
        self.root.configure(background=COLORS["bg"])
        self._set_window_icon()

        self.events: queue.Queue[tuple[str, Any, int]] = queue.Queue()
        self.engine = CaptureEngine(self.events)
        self.generation = 0  # engine run whose events the UI applies
        self.switching = False  # a source change is waiting for the old capture to end
        self.destroyed = False
        self.shortcut_held = False
        self.devices: list[AudioDevice] = []
        self.microphones: list[AudioDevice] = []
        self.settings = load_settings()
        self.closing = False
        self.state = "idle"  # idle | starting | recording | saving
        self.last_saved: Path | None = None
        self.capture_sources: tuple[AudioDevice | None, AudioDevice | None] = (None, None)
        scope_points = int(self.SCOPE_SECONDS * SAMPLE_RATE / BLOCK_SIZE * ENVELOPE_POINTS_PER_BLOCK)
        self.scope_history: deque[float] = deque([0.0] * scope_points, maxlen=scope_points)
        self.last_level_at = 0.0
        self.sound_detected = False
        self.blink_on = True

        self.device_var = tk.StringVar()
        self.include_microphone_var = tk.BooleanVar(value=bool(self.settings.get("include_microphone", False)))
        self.microphone_var = tk.StringVar()
        saved_output = str(self.settings.get("output_dir") or "")
        self.output_var = tk.StringVar(value=saved_output or str(default_output_dir()))
        self.status_var = tk.StringVar(value="Ready")
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.meter_var = tk.StringVar(value="Starting audio preview…")
        self.saved_var = tk.StringVar(value="")
        self.notice_var = tk.StringVar(value="")

        self._configure_styles()
        self._build_ui()
        self.refresh_devices(initial=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind_all("<Control-KeyPress-r>", self._on_shortcut)
        self.root.bind_all("<KeyRelease-r>", self._on_shortcut_release)
        self._fit_to_screen()
        self.root.after(50, self._poll_events)
        self.root.after(500, self._blink)

    # ----- layout helpers -------------------------------------------------

    def _fit_to_screen(self) -> None:
        """Size the window to its content, compacting it on short screens."""
        self.root.update_idletasks()
        available = self.root.winfo_screenheight() - self.px(90)  # title bar and taskbar
        if self.root.winfo_reqheight() > available:
            self.footer.grid_remove()
            self.scope.configure(height=self.px(44))
            self.root.update_idletasks()
        width = max(self.px(660), self.root.winfo_reqwidth())
        height = min(self.root.winfo_reqheight(), available)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(self.px(560), min(height, self.px(560)))

    def _grow_to_fit(self) -> None:
        """Grow the window when new lines appear, never past the screen."""
        if self.destroyed:
            return
        self.root.update_idletasks()
        available = self.root.winfo_screenheight() - self.px(90)
        wanted = min(self.root.winfo_reqheight(), available)
        if self.root.winfo_height() < wanted and self.root.state() == "normal":
            self.root.geometry(f"{self.root.winfo_width()}x{wanted}")

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def _set_window_icon(self) -> None:
        try:
            size = 32
            icon = tk.PhotoImage(width=size, height=size)
            center = (size - 1) / 2
            rows = []
            for y in range(size):
                row = []
                for x in range(size):
                    distance = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
                    if distance <= 9:
                        row.append(COLORS["rec"])
                    elif distance <= 15:
                        row.append("#1f2937")
                    else:
                        row.append("")
                rows.append(row)
            for y, row in enumerate(rows):
                for x, color in enumerate(row):
                    if color:
                        icon.put(color, (x, y))
            self._icon = icon
            self.root.iconphoto(True, icon)
        except tk.TclError:
            pass

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        c = COLORS
        base = (UI_FONT, 10)
        self.root.option_add("*TCombobox*Listbox.font", base)
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
        style.configure(".", background=c["bg"], foreground=c["text"], font=base)
        style.configure("Card.TFrame", background=c["card"])
        style.configure("Bg.TFrame", background=c["bg"])
        style.configure("Card.TLabel", background=c["card"], foreground=c["text"])
        style.configure("Muted.TLabel", background=c["card"], foreground=c["muted"])
        style.configure("Title.TLabel", background=c["bg"], foreground=c["text"], font=(UI_FONT, 17, "bold"))
        style.configure("Subtitle.TLabel", background=c["bg"], foreground=c["muted"], font=(UI_FONT, 10))
        style.configure("Footer.TLabel", background=c["bg"], foreground=c["subtle"], font=(UI_FONT, 9))
        style.configure(
            "Section.TLabel", background=c["card"], foreground=c["muted"], font=(UI_FONT, 9, "bold")
        )
        style.configure("Status.TLabel", background=c["card"], foreground=c["muted"], font=(UI_FONT, 10, "bold"))
        style.configure("Timer.TLabel", background=c["card"], foreground=c["text"], font=(f"{UI_FONT} Light", 32))
        style.configure("Caption.TLabel", background=c["card"], foreground=c["muted"], font=(UI_FONT, 9))
        style.configure("Saved.TLabel", background=c["card"], foreground=c["ok"], font=(UI_FONT, 9))
        style.configure("Notice.TLabel", background=c["card"], foreground=c["warn"], font=(UI_FONT, 9))

        style.configure(
            "TCheckbutton",
            background=c["card"],
            foreground=c["text"],
            indicatorbackground=c["card"],
            indicatorforeground=c["accent"],
            indicatorsize=self.px(13),
            indicatormargin=(0, 0, self.px(6), 0),
            focuscolor=c["accent"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", c["card"])],
            foreground=[("disabled", c["subtle"])],
            # Disabled states first: ttk uses the first matching entry.
            indicatorbackground=[
                ("disabled", "selected", c["subtle"]),
                ("disabled", c["bg"]),
                ("selected", c["accent"]),
            ],
            indicatorforeground=[("selected", "#ffffff")],
        )
        style.configure(
            "TCombobox",
            padding=(8, 5),
            arrowsize=self.px(13),
            fieldbackground=c["card"],
            background=c["card"],
            bordercolor=c["border"],
            lightcolor=c["card"],
            darkcolor=c["card"],
            arrowcolor=c["muted"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["card"]), ("disabled", c["bg"])],
            foreground=[("disabled", c["subtle"])],
            selectbackground=[("readonly", c["card"])],
            selectforeground=[("readonly", c["text"])],
            bordercolor=[("focus", c["accent"])],
            arrowcolor=[("disabled", c["subtle"])],
        )
        style.configure(
            "TEntry",
            padding=(8, 5),
            fieldbackground=c["card"],
            bordercolor=c["border"],
            lightcolor=c["card"],
            darkcolor=c["card"],
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", c["bg"])],
            foreground=[("disabled", c["subtle"])],
            bordercolor=[("focus", c["accent"])],
        )
        style.configure(
            "TButton",
            padding=(12, 5),
            background=c["card"],
            foreground=c["text"],
            bordercolor=c["border"],
            lightcolor=c["card"],
            darkcolor=c["card"],
            focuscolor=c["card"],
        )
        style.map(
            "TButton",
            background=[("pressed", "#e5e7eb"), ("active", "#f3f4f6"), ("disabled", c["bg"])],
            foreground=[("disabled", c["subtle"])],
            bordercolor=[("focus", c["accent"])],
        )
        style.configure(
            "Link.TButton",
            padding=(4, 0),
            background=c["card"],
            foreground=c["accent"],
            borderwidth=0,
            bordercolor=c["card"],
            lightcolor=c["card"],
            darkcolor=c["card"],
            focuscolor=c["accent"],
            font=(UI_FONT, 9),
        )
        style.map(
            "Link.TButton",
            background=[("active", c["card"]), ("pressed", c["card"])],
            foreground=[("active", "#1d4ed8"), ("disabled", c["subtle"])],
        )

    def _card(self, parent: tk.Misc, row: int, pady: tuple[int, int]) -> ttk.Frame:
        outer = tk.Frame(
            parent,
            background=COLORS["card"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["border"],
        )
        outer.grid(row=row, column=0, sticky="ew", pady=pady)
        inner = ttk.Frame(outer, style="Card.TFrame", padding=self.px(16))
        inner.pack(fill="both", expand=True)
        inner.columnconfigure(1, weight=1)
        return inner

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, style="Bg.TFrame", padding=(self.px(22), self.px(18)))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        gap = self.px(12)

        # Header
        header = ttk.Frame(frame, style="Bg.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, self.px(14)))
        ttk.Label(header, text="Meeting Recorder", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Records what plays through your speakers or headset — and, optionally, your microphone.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w")

        # Hero: timer, record button, live scope
        hero = self._card(frame, 1, (0, gap))
        hero.columnconfigure(0, weight=1)
        top = ttk.Frame(hero, style="Card.TFrame")
        top.grid(row=0, column=0, columnspan=3, sticky="ew")
        top.columnconfigure(0, weight=1)

        status_row = ttk.Frame(top, style="Card.TFrame")
        status_row.grid(row=0, column=0, sticky="w")
        dot = self.px(10)
        self.status_dot = tk.Canvas(
            status_row, width=dot, height=dot, background=COLORS["card"], highlightthickness=0
        )
        self.status_dot.grid(row=0, column=0, padx=(0, self.px(6)))
        self.status_label = ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel")
        self.status_label.grid(row=0, column=1, sticky="w")
        ttk.Label(top, textvariable=self.elapsed_var, style="Timer.TLabel").grid(
            row=1, column=0, sticky="w"
        )

        self.record_button = RecordButton(top, self.px(68), self.toggle_recording)
        self.record_button.grid(row=0, column=1, rowspan=2, sticky="e")
        self.record_hint = ttk.Label(top, text="Start recording", style="Caption.TLabel")
        self.record_hint.grid(row=2, column=1, sticky="e", pady=(self.px(4), 0))

        self.scope = tk.Canvas(
            hero,
            height=self.px(84),
            background=COLORS["scope_bg"],
            highlightthickness=0,
        )
        self.scope.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(self.px(12), self.px(8)))
        self.scope.bind("<Configure>", lambda _e: self._draw_scope())

        caption = ttk.Frame(hero, style="Card.TFrame")
        caption.grid(row=2, column=0, columnspan=3, sticky="ew")
        caption.columnconfigure(0, weight=1)
        ttk.Label(caption, textvariable=self.meter_var, style="Caption.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.saved_label = ttk.Label(caption, textvariable=self.saved_var, style="Saved.TLabel")
        self.saved_label.grid(row=1, column=0, sticky="w", pady=(self.px(2), 0))
        self.notice_label = ttk.Label(
            caption, textvariable=self.notice_var, style="Notice.TLabel", wraplength=self.px(560)
        )
        self.notice_label.grid(row=2, column=0, columnspan=2, sticky="w")
        # Collapse the result/notice lines while they are empty.
        for label, variable in ((self.saved_label, self.saved_var), (self.notice_label, self.notice_var)):
            def sync(*_args: Any, label: ttk.Label = label, variable: tk.StringVar = variable) -> None:
                if variable.get():
                    label.grid()
                    self.root.after_idle(self._grow_to_fit)
                else:
                    label.grid_remove()

            variable.trace_add("write", sync)
            sync()
        self.reveal_button = ttk.Button(
            caption, text="Show in folder", style="Link.TButton", command=self.reveal_last_saved
        )
        self.reveal_button.grid(row=1, column=1, sticky="e")
        self.reveal_button.grid_remove()

        # Sources
        sources = self._card(frame, 2, (0, gap))
        ttk.Label(sources, text="SOURCES", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.refresh_button = ttk.Button(
            sources, text="↻  Refresh devices", style="Link.TButton", command=self.refresh_devices
        )
        self.refresh_button.grid(row=0, column=1, columnspan=2, sticky="e")

        ttk.Label(sources, text="System audio", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=(self.px(10), 0), padx=(0, self.px(12))
        )
        self.device_box = ttk.Combobox(sources, textvariable=self.device_var, state="readonly")
        self.device_box.grid(row=1, column=1, sticky="ew", pady=(self.px(10), 0))
        self.device_box.bind("<<ComboboxSelected>>", self._on_source_changed)
        self.system_meter = LevelMeter(sources, self.px(70), self.px(6))
        self.system_meter.grid(row=1, column=2, sticky="e", padx=(self.px(12), 0), pady=(self.px(10), 0))

        self.include_microphone_check = ttk.Checkbutton(
            sources,
            text="Microphone",
            variable=self.include_microphone_var,
            command=self.on_microphone_toggle,
        )
        self.include_microphone_check.grid(
            row=2, column=0, sticky="w", pady=(self.px(10), 0), padx=(0, self.px(12))
        )
        self.microphone_box = ttk.Combobox(sources, textvariable=self.microphone_var, state="disabled")
        self.microphone_box.grid(row=2, column=1, sticky="ew", pady=(self.px(10), 0))
        self.microphone_box.bind("<<ComboboxSelected>>", self._on_source_changed)
        self.microphone_meter = LevelMeter(sources, self.px(70), self.px(6))
        self.microphone_meter.grid(
            row=2, column=2, sticky="e", padx=(self.px(12), 0), pady=(self.px(10), 0)
        )
        self.microphone_meter.set_enabled(False)

        # Output folder
        output = self._card(frame, 3, (0, gap))
        ttk.Label(output, text="SAVE RECORDINGS TO", style="Section.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        self.output_entry = ttk.Entry(output, textvariable=self.output_var)
        self.output_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(self.px(10), 0))
        self.browse_button = ttk.Button(output, text="Browse…", command=self.choose_folder)
        self.browse_button.grid(row=1, column=2, padx=(self.px(8), 0), pady=(self.px(10), 0))
        self.open_folder_button = ttk.Button(output, text="Open", command=self.open_output_folder)
        self.open_folder_button.grid(row=1, column=3, padx=(self.px(6), 0), pady=(self.px(10), 0))
        output.columnconfigure(1, weight=1)
        ttk.Label(
            output,
            text="48 kHz · 16-bit WAV · named by date and time",
            style="Caption.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(self.px(6), 0))

        self.footer = ttk.Label(
            frame,
            text=(
                "Use headphones when recording your microphone to avoid echo. "
                "Get consent and follow your meeting's recording policy.   Ctrl+R starts or stops."
            ),
            style="Footer.TLabel",
            wraplength=self.px(600),
            justify="left",
        )
        self.footer.grid(row=4, column=0, sticky="w", pady=(self.px(2), 0))

        self._apply_state()

    # ----- devices --------------------------------------------------------

    def _selected_sources(self) -> tuple[AudioDevice | None, AudioDevice | None]:
        device_index = self.device_box.current()
        device = self.devices[device_index] if 0 <= device_index < len(self.devices) else None
        microphone = None
        if self.include_microphone_var.get():
            microphone_index = self.microphone_box.current()
            if 0 <= microphone_index < len(self.microphones):
                microphone = self.microphones[microphone_index]
        return device, microphone

    def refresh_devices(self, initial: bool = False) -> None:
        if self.engine.recording or self.state != "idle":
            return
        if not self.engine.stop_and_wait(timeout=2.0):
            self.notice_var.set("Audio preview is still closing — try Refresh again in a moment.")
            self.switching = True  # keeps Start disabled; the poller restarts the preview
            self._apply_state()
            return
        selected_id = None
        current_index = self.device_box.current()
        if 0 <= current_index < len(self.devices):
            selected_id = self.devices[current_index].device_id
        elif initial:
            selected_id = self.settings.get("device_id")

        selected_microphone_name = None
        current_microphone_index = self.microphone_box.current()
        if 0 <= current_microphone_index < len(self.microphones):
            selected_microphone_name = self.microphones[current_microphone_index].name
        elif initial:
            selected_microphone_name = self.settings.get("microphone_name")

        try:
            self.devices = available_playback_devices()
            self.device_box["values"] = [d.name for d in self.devices]
            ids = [d.device_id for d in self.devices]
            preferred_id = selected_id if selected_id in ids else default_device_id()
            preferred_index = ids.index(preferred_id) if preferred_id in ids else 0
            if self.devices:
                self.device_box.current(preferred_index)
            else:
                self.device_var.set("")
            self.status_var.set("Ready" if self.devices else "No playback device found")
        except Exception as exc:
            self.devices = []
            self.device_box["values"] = []
            self.device_var.set("")
            self.status_var.set("Could not list playback devices")
            messagebox.showerror(APP_TITLE, f"Could not list playback devices:\n\n{exc}")

        # Microphones are optional: a failure here must not block system audio.
        try:
            if not initial:
                reload_portaudio()  # pick up microphones plugged in since launch
            self.microphones = available_microphones()
            self.microphone_box["values"] = [d.name for d in self.microphones]
            names = [m.name for m in self.microphones]
            mic_ids = [m.device_id for m in self.microphones]
            if selected_microphone_name in names:
                mic_index = names.index(selected_microphone_name)
            else:
                default_mic = default_microphone_id()
                mic_index = mic_ids.index(default_mic) if default_mic in mic_ids else 0
            if self.microphones:
                self.microphone_box.current(mic_index)
            else:
                self.microphone_var.set("")
                self.include_microphone_var.set(False)
            if self.notice_var.get().startswith("Microphones unavailable"):
                self.notice_var.set("")
        except Exception as exc:
            self.microphones = []
            self.microphone_box["values"] = []
            self.microphone_var.set("")
            self.include_microphone_var.set(False)
            self.notice_var.set(f"Microphones unavailable: {exc}")
        self._apply_state()
        self._start_capture()

    def on_microphone_toggle(self) -> None:
        self._apply_state()
        self._restart_capture()

    def _on_source_changed(self, _event: tk.Event | None = None) -> None:
        self._restart_capture()

    def _reset_meters(self) -> None:
        self.system_meter.set_level(None, immediate=True)
        self.microphone_meter.set_level(None, immediate=True)
        self.scope_history.extend([0.0] * self.scope_history.maxlen)  # type: ignore[arg-type]
        self.sound_detected = False
        self.last_level_at = 0.0
        self._draw_scope()

    def _start_capture(self) -> None:
        if self.closing or self.engine.running:
            return
        self.switching = False
        device, microphone = self._selected_sources()
        self.capture_sources = (device, microphone)
        self.microphone_meter.set_enabled(microphone is not None)
        self._reset_meters()
        if device is None:
            self.meter_var.set("Select a playback device to preview its audio.")
            return
        self.meter_var.set("Starting audio preview…")
        try:
            self.generation = self.engine.start(device, microphone)
        except Exception as exc:
            self.notice_var.set(f"Audio preview unavailable: {exc}")
        self._save_settings()

    def _restart_capture(self) -> None:
        """Switch the preview to the selected sources without blocking the window."""
        if self.closing or self.state != "idle" or self.engine.recording:
            return
        self.engine.request_stop()
        if self.engine.stop_and_wait(timeout=0.5):
            self._start_capture()
        else:
            # Finish the switch from the event loop once the old capture has ended.
            self.switching = True
            self.meter_var.set("Switching audio source…")
            self._apply_state()

    # ----- recording ------------------------------------------------------

    def toggle_recording(self) -> None:
        if self.state == "idle":
            self.start_recording()
        elif self.state in ("starting", "recording"):
            self.stop_recording()

    def _on_shortcut(self, _event: tk.Event) -> str:
        # Key auto-repeat must not turn one press into Stop and a new Start.
        if not self.shortcut_held:
            self.shortcut_held = True
            self.toggle_recording()
        return "break"

    def _on_shortcut_release(self, _event: tk.Event) -> None:
        self.shortcut_held = False

    def start_recording(self) -> None:
        if self.state != "idle" or self.switching:
            return
        device, microphone = self._selected_sources()
        if device is None:
            messagebox.showerror(APP_TITLE, "Select a playback device first.")
            return
        if self.include_microphone_var.get() and microphone is None:
            messagebox.showerror(APP_TITLE, "Select a microphone first.")
            return
        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showerror(APP_TITLE, "Choose an output folder first.")
            return

        try:
            output_dir = Path(os.path.expandvars(output_text)).expanduser().resolve()
            if not self.engine.running or self.capture_sources != (device, microphone):
                if not self.engine.stop_and_wait(timeout=2.0):
                    raise RuntimeError("The previous audio capture is still closing. Try again.")
                self.generation = self.engine.start(device, microphone)
                self.capture_sources = (device, microphone)
            planned_path = self.engine.begin_recording(output_dir)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not start recording:\n\n{exc}")
            self._start_capture()
            return

        self.output_var.set(str(output_dir))
        self._save_settings()
        self.state = "starting"
        self.elapsed_var.set("00:00:00")
        self.saved_var.set("")
        self.notice_var.set(MICROPHONE_LOST_NOTICE if microphone and not self.engine.microphone_ok else "")
        self.reveal_button.grid_remove()
        self.status_var.set(f"Starting {planned_path.name}…")
        self._apply_state()

    def stop_recording(self) -> None:
        if self.state not in ("starting", "recording"):
            return
        self.state = "saving"
        self.status_var.set("Saving…")
        self.engine.end_recording()
        self._apply_state()

    def _finish_recording(self, paths: list[Path], seconds: float, notes: list[str] | None = None) -> None:
        self.state = "idle"
        self.elapsed_var.set(format_duration(seconds))
        if paths:
            self.last_saved = paths[-1]
            names = paths[0].name if len(paths) == 1 else f"{paths[0].name} (+{len(paths) - 1} more parts)"
            self.saved_var.set(f"Saved {names}  ·  {format_duration(seconds)}")
            self.reveal_button.grid()
            self.status_var.set("Saved")
        else:
            self.saved_var.set("")
            self.reveal_button.grid_remove()
            self.status_var.set("Ready")
        if notes:
            self.notice_var.set("⚠  " + " ".join(notes) + " The audio is kept in the file shown above.")
        self._apply_state()

    # ----- state & settings ----------------------------------------------

    def _apply_state(self) -> None:
        busy = self.state != "idle"
        has_device = bool(self.devices)
        self.record_button.configure_state(
            recording=self.state in ("starting", "recording", "saving"),
            enabled=has_device and self.state != "saving" and not self.switching and not self.closing,
        )
        self.record_hint.configure(
            text={
                "idle": "Start recording",
                "starting": "Stop and save",
                "recording": "Stop and save",
                "saving": "Saving…",
            }[self.state]
        )
        self.device_box.configure(state="disabled" if busy or not has_device else "readonly")
        self.refresh_button.configure(state="disabled" if busy or self.switching else "normal")
        self.include_microphone_check.configure(
            state="disabled" if busy or not self.microphones else "normal"
        )
        self.microphone_box.configure(
            state=(
                "readonly"
                if not busy and self.include_microphone_var.get() and self.microphones
                else "disabled"
            )
        )
        self.output_entry.configure(state="disabled" if busy else "normal")
        self.browse_button.configure(state="disabled" if busy else "normal")
        self._draw_status_dot()

    def _save_settings(self) -> None:
        device, _ = self._selected_sources()
        microphone_index = self.microphone_box.current()
        microphone_name = (
            self.microphones[microphone_index].name
            if 0 <= microphone_index < len(self.microphones)
            else self.settings.get("microphone_name")
        )
        self.settings.update(
            {
                "device_id": device.device_id if device else self.settings.get("device_id"),
                "microphone_name": microphone_name,
                "include_microphone": bool(self.include_microphone_var.get()),
                "output_dir": self.output_var.get().strip(),
            }
        )
        save_settings(self.settings)

    def choose_folder(self) -> None:
        initial = self.output_var.get().strip()
        chosen = filedialog.askdirectory(
            initialdir=initial if initial and Path(initial).is_dir() else str(Path.home())
        )
        if chosen:
            self.output_var.set(str(Path(chosen)))
            self._save_settings()

    def open_output_folder(self) -> None:
        folder = Path(os.path.expandvars(self.output_var.get().strip() or ".")).expanduser()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not open the folder:\n\n{exc}")
            return
        reveal_in_explorer(folder)

    def reveal_last_saved(self) -> None:
        if self.last_saved is not None:
            reveal_in_explorer(self.last_saved if self.last_saved.exists() else self.last_saved.parent)

    # ----- events & drawing ----------------------------------------------

    def _poll_events(self) -> None:
        if self.destroyed:
            return
        try:
            self._handle_events()
        finally:
            if not self.destroyed:
                self.root.after(50, self._poll_events)

    def _handle_events(self) -> None:
        latest_level = None
        for _ in range(500):
            try:
                event, payload, generation = self.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                # A replaced capture. Only a finished recording still matters.
                stale_paths = payload[0] if event == "saved" else payload[1] if event == "error" else []
                if stale_paths:
                    self.notice_var.set(
                        "An earlier recording was saved to " + ", ".join(str(p) for p in stale_paths)
                    )
                continue
            if event == "level":
                system_level, microphone_level, env, seconds = payload
                self.scope_history.extend(float(v) for v in env)
                latest_level = (system_level, microphone_level, seconds)
            elif event == "capturing":
                self.meter_var.set("Listening — waiting for sound")
                if self.notice_var.get().startswith("Audio preview unavailable"):
                    self.notice_var.set("")
            elif event == "recording":
                if self.state == "starting":
                    self.state = "recording"
                    self.status_var.set("Recording")
                    self._apply_state()
            elif event == "microphone":
                if not payload:
                    self.notice_var.set(MICROPHONE_LOST_NOTICE)
                elif self.notice_var.get() == MICROPHONE_LOST_NOTICE:
                    self.notice_var.set("")
            elif event == "saved":
                paths, seconds, notes = payload
                self._finish_recording(list(paths), seconds, list(notes))
                if self.closing and notes:
                    messagebox.showwarning(APP_TITLE, "\n\n".join([*notes, *(str(p) for p in paths)]))
            elif event == "error":
                exc, paths, seconds = payload
                latest_level = None
                was_busy = self.state != "idle"
                self._finish_recording(list(paths), seconds)
                if was_busy or paths:
                    self.status_var.set("Recording stopped by an error")
                    self._apply_state()
                    saved_note = (
                        "\n\nAudio captured before the error was saved to:\n"
                        + "\n".join(str(p) for p in paths)
                        if paths
                        else "\n\nNo audio could be saved."
                    )
                    # Shown even while closing: the user must know the save failed.
                    messagebox.showerror(APP_TITLE, f"Recording failed:\n\n{exc}{saved_note}")
                else:
                    self.notice_var.set(f"Audio preview unavailable: {exc}")
            elif event == "capture_stopped":
                latest_level = None
                self._reset_meters()
                if self.state != "idle":
                    self._finish_recording([], 0.0)
                if self.closing:
                    self._close_now()
                    return

        if self.switching and not self.engine.running and not self.closing:
            self.switching = False
            self._start_capture()
            self._apply_state()

        if latest_level is not None:
            system_level, microphone_level, seconds = latest_level
            self.system_meter.set_level(system_level)
            self.microphone_meter.set_level(microphone_level)
            if seconds is not None and self.state in ("starting", "recording"):
                self.elapsed_var.set(format_duration(seconds))
            detected = max(system_level, microphone_level or 0.0) >= 0.001
            if detected:
                self.last_level_at = time.monotonic()
            self.sound_detected = time.monotonic() - self.last_level_at < 1.0
            mode = "Recording" if self.state in ("starting", "recording") else "Listening"
            self.meter_var.set(f"{mode} — sound detected" if self.sound_detected else f"{mode} — waiting for sound")
            self._draw_scope()

    def _blink(self) -> None:
        if self.destroyed:
            return
        self.blink_on = not self.blink_on
        self._draw_status_dot()
        self.root.after(500, self._blink)

    def _draw_status_dot(self) -> None:
        if not hasattr(self, "status_dot"):
            return
        size = int(self.status_dot["width"])
        if self.state in ("starting", "recording"):
            color = COLORS["rec"] if self.blink_on else "#fca5a5"
            self.status_label.configure(foreground=COLORS["rec"])
        elif self.state == "saving":
            color = COLORS["warn"]
            self.status_label.configure(foreground=COLORS["warn"])
        elif self.status_var.get() == "Saved":
            color = COLORS["ok"]
            self.status_label.configure(foreground=COLORS["ok"])
        else:
            color = COLORS["idle_dot"]
            self.status_label.configure(foreground=COLORS["muted"])
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, size - 1, size - 1, fill=color, outline="")

    def _draw_scope(self) -> None:
        canvas = self.scope
        width = max(2, canvas.winfo_width())
        height = max(2, canvas.winfo_height())
        center = height / 2
        canvas.delete("all")
        canvas.create_line(0, center, width, center, fill=COLORS["scope_axis"])
        values = list(self.scope_history)
        if not values:
            return
        # Scale to the recent peak so quiet audio is visible, down to -40 dBFS.
        top = max(max(values), 0.01)
        amplitude = height * 0.44 / top
        step = width / max(1, len(values) - 1)
        upper: list[float] = []
        lower: list[float] = []
        for index, value in enumerate(values):
            x = index * step
            offset = max(0.6, value * amplitude)
            upper.extend((x, center - offset))
            lower.extend((x, center + offset))
        points = upper + [coord for pair in reversed(list(zip(lower[0::2], lower[1::2]))) for coord in pair]
        if self.state in ("starting", "recording"):
            color = COLORS["scope_rec"]
        elif self.sound_detected:
            color = COLORS["scope_live"]
        else:
            color = COLORS["scope_quiet"]
        if len(points) >= 6:
            canvas.create_polygon(*points, fill=color, outline=color)

    # ----- closing --------------------------------------------------------

    def on_close(self) -> None:
        if self.closing:
            return
        if self.state in ("starting", "recording", "saving"):
            if not messagebox.askyesno(APP_TITLE, "A recording is in progress. Stop, save, and close?"):
                return
            if self.state == "idle":  # it finished while the dialog was open
                self.on_close()
                return
            self.closing = True
            self.state = "saving"
            self.status_var.set("Saving before closing…")
            self._apply_state()
            self.engine.end_recording()
            self.engine.request_stop()
            # The window closes when the capture reports it has stopped.
            self.root.after(15_000, self._close_timeout)
        else:
            self.closing = True
            self.engine.request_stop()
            self._save_settings()
            self._close_now()

    def _close_timeout(self) -> None:
        if self.destroyed:
            return
        if messagebox.askyesno(
            APP_TITLE,
            "Saving is taking longer than expected (the audio device may have stopped responding).\n\n"
            "Close anyway? Audio written so far stays in the .recording.wav file in your recordings folder.",
        ):
            self._close_now()
        else:
            self.root.after(15_000, self._close_timeout)

    def _close_now(self) -> None:
        self.destroyed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _safe_console() -> None:
    """Avoid UnicodeEncodeError for non-ASCII device names on legacy consoles."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def self_test() -> int:
    devices = available_playback_devices()
    print(f"Found {len(devices)} playback device(s).")
    for index, device in enumerate(devices, start=1):
        native = f"{device.native_channels} channel(s)" if device.native_channels else "channels unknown"
        print(f"{index}. {device.name} [{native}; captured as stereo]")
    if not devices:
        print("ERROR: Windows did not report any playback devices.", file=sys.stderr)
        return 1
    try:
        microphones = available_microphones()
        print(f"Found {len(microphones)} microphone(s).")
        for index, microphone in enumerate(microphones, start=1):
            api = "WASAPI" if microphone.wasapi else "non-WASAPI"
            print(f"{index}. {microphone.name} [{api}]")
    except Exception as exc:
        print(f"WARNING: microphones unavailable ({exc}); system audio still works.")
    print("Recorder dependencies and device discovery are working.")
    return 0


def timed_recording(seconds: float, output_dir: Path, with_microphone: bool = False) -> int:
    """Internal packaged-build smoke test: record, then verify the saved file."""
    if not (math.isfinite(seconds) and seconds > 0):
        print("Timed test needs a positive, finite duration.", file=sys.stderr)
        return 1
    engine: CaptureEngine | None = None
    try:
        devices = available_playback_devices()
        if not devices:
            print("Timed test found no playback device.", file=sys.stderr)
            return 1
        selected_id = default_device_id()
        device = next((item for item in devices if item.device_id == selected_id), devices[0])
        microphone = None
        if with_microphone:
            microphones = available_microphones()
            default_mic = default_microphone_id()
            microphone = next((m for m in microphones if m.device_id == default_mic), None) or (
                microphones[0] if microphones else None
            )
            if microphone is None:
                print("Timed test found no microphone.", file=sys.stderr)
                return 1
        events: queue.Queue[tuple[str, Any, int]] = queue.Queue()
        engine = CaptureEngine(events)
        engine.start(device, microphone)

        def next_event(deadline: float) -> tuple[str, Any]:
            event, payload, _generation = events.get(timeout=max(0.01, deadline - time.monotonic()))
            return event, payload

        deadline = time.monotonic() + 15
        while True:
            event, payload = next_event(deadline)
            if event == "capturing":
                break
            if event in ("error", "capture_stopped"):
                print(f"Timed test could not start: {event}: {payload!r}", file=sys.stderr)
                return 1
        engine.begin_recording(output_dir)
        time.sleep(seconds)
        engine.end_recording()
        deadline = time.monotonic() + 15
        while True:
            event, payload = next_event(deadline)
            if event == "error":
                print(f"Timed test could not finalize: {payload!r}", file=sys.stderr)
                return 1
            if event == "saved":
                break
        paths, duration, notes = payload
        if not paths or notes:
            print(f"Timed test saved no usable file: {paths!r} {notes!r}", file=sys.stderr)
            return 1
        frames = 0
        for path in paths:
            info = sf.info(str(path))
            if (info.samplerate, info.channels, info.subtype) != (SAMPLE_RATE, device.channels, "PCM_16"):
                print(f"Timed test wrote an unexpected format: {info}", file=sys.stderr)
                return 1
            frames += info.frames
        recorded = frames / SAMPLE_RATE
        print(f"Saved {', '.join(str(p) for p in paths)} ({recorded:.2f} s, {device.channels} ch)")
        # Start and Stop act on 100 ms blocks; allow for that and scheduling.
        if abs(recorded - seconds) > 0.35 or abs(recorded - duration) > 1e-6:
            print(f"Timed test duration mismatch: asked {seconds} s, saved {recorded:.2f} s", file=sys.stderr)
            return 1
        return 0
    except queue.Empty:
        print("Timed test timed out waiting for the audio device.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Timed test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.stop_and_wait()


def main() -> int:
    _safe_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify dependencies and list playback devices without recording",
    )
    parser.add_argument("--record-seconds", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--with-microphone", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.record_seconds is not None:
        output_dir = (args.output_dir or (application_dir() / "recordings")).resolve()
        return timed_recording(args.record_seconds, output_dir, args.with_microphone)

    enable_dpi_awareness()
    root = tk.Tk()
    app = RecorderApp(root)
    root.mainloop()
    # Let an in-flight recording finish writing its file before the process exits.
    app.engine.request_stop()
    return 0 if app.engine.stop_and_wait(timeout=10.0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
