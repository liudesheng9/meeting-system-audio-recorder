"""Small Windows GUI for recording system playback audio with WASAPI loopback."""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import soundcard as sc
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "Meeting System Audio Recorder"
SAMPLE_RATE = 48_000
BLOCK_SIZE = 4_800  # 100 ms at 48 kHz


def application_dir() -> Path:
    """Folder containing the script, or the packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


@dataclass(frozen=True)
class PlaybackDevice:
    name: str
    device_id: str
    channels: int


def available_playback_devices() -> list[PlaybackDevice]:
    devices: list[PlaybackDevice] = []
    for speaker in sc.all_speakers():
        channels = int(getattr(speaker, "channels", 2) or 2)
        devices.append(
            PlaybackDevice(
                name=str(speaker.name),
                device_id=str(speaker.id),
                channels=max(1, min(channels, 2)),
            )
        )
    return devices


def available_microphones() -> list[PlaybackDevice]:
    device_info = list(sd.query_devices())
    host_apis = list(sd.query_hostapis())
    wasapi_indexes = {
        index
        for index, host_api in enumerate(host_apis)
        if "WASAPI" in str(host_api["name"]).upper()
    }
    candidates = [
        (index, info)
        for index, info in enumerate(device_info)
        if int(info["max_input_channels"]) > 0
        and (not wasapi_indexes or int(info["hostapi"]) in wasapi_indexes)
    ]
    devices: list[PlaybackDevice] = []
    for index, microphone in candidates:
        host_api_name = str(host_apis[int(microphone["hostapi"])]["name"])
        devices.append(
            PlaybackDevice(
                name=f"{microphone['name']} — {host_api_name}",
                device_id=str(index),
                channels=int(microphone["max_input_channels"]),
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
        default_index = int(sd.default.device[0])
        default_name = str(sd.query_devices(default_index)["name"])
        microphones = available_microphones()
        matching = next(
            (microphone for microphone in microphones if microphone.name.startswith(default_name)),
            None,
        )
        return matching.device_id if matching else (microphones[0].device_id if microphones else None)
    except Exception:
        return None


def safe_filename_timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("meeting_%Y-%m-%d_%H-%M-%S")


def unused_recording_paths(output_dir: Path) -> tuple[Path, Path]:
    """Return final/partial paths without overwriting any earlier recording."""
    stem = safe_filename_timestamp()
    suffix = 1
    while True:
        candidate_stem = stem if suffix == 1 else f"{stem}_{suffix}"
        final_path = output_dir / f"{candidate_stem}.wav"
        partial_path = output_dir / f"{candidate_stem}.recording.wav"
        if not final_path.exists() and not partial_path.exists():
            return final_path, partial_path
        suffix += 1


def waveform_payload(data: np.ndarray, points: int = 80) -> np.ndarray:
    """Return a small mono waveform suitable for passing to the Tk UI."""
    if data.size == 0:
        return np.zeros(points, dtype=np.float32)
    mono = np.mean(data, axis=1) if data.ndim > 1 else data
    indexes = np.linspace(0, len(mono) - 1, points, dtype=int)
    return np.asarray(mono[indexes], dtype=np.float32)


class RecorderWorker:
    """Captures one playback endpoint and writes a finalized WAV file."""

    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        self.events = events
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(
        self,
        device: PlaybackDevice,
        output_dir: Path,
        microphone: PlaybackDevice | None = None,
    ) -> Path:
        if self.running:
            raise RuntimeError("A recording is already in progress.")

        output_dir.mkdir(parents=True, exist_ok=True)
        final_path, partial_path = unused_recording_paths(output_dir)
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._capture,
            args=(device, microphone, partial_path, final_path),
            daemon=False,
            name="system-audio-capture",
        )
        self.thread.start()
        return final_path

    def stop(self) -> None:
        self.stop_event.set()

    def _capture(
        self,
        device: PlaybackDevice,
        microphone: PlaybackDevice | None,
        partial_path: Path,
        final_path: Path,
    ) -> None:
        started = time.monotonic()
        try:
            loopback = sc.get_microphone(device.device_id, include_loopback=True)
            if loopback is None:
                raise RuntimeError("Windows could not open loopback capture for this device.")

            with sf.SoundFile(
                partial_path,
                mode="w",
                samplerate=SAMPLE_RATE,
                channels=device.channels,
                subtype="PCM_16",
                format="WAV",
            ) as wav_file:
                with loopback.recorder(
                    samplerate=SAMPLE_RATE,
                    channels=device.channels,
                    blocksize=BLOCK_SIZE,
                ) as system_recorder:
                    if microphone is None:
                        self._record_blocks(wav_file, system_recorder, None)
                    else:
                        with sd.InputStream(
                            device=int(microphone.device_id),
                            samplerate=SAMPLE_RATE,
                            channels=1,
                            blocksize=BLOCK_SIZE,
                            dtype="float32",
                        ) as microphone_recorder:
                            self._record_blocks(
                                wav_file,
                                system_recorder,
                                microphone_recorder,
                            )

            os.replace(partial_path, final_path)
            self.events.put(("stopped", (final_path, time.monotonic() - started)))
        except Exception as exc:
            self.events.put(("error", (exc, partial_path if partial_path.exists() else None)))

    def _record_blocks(
        self,
        wav_file: sf.SoundFile,
        system_recorder: Any,
        microphone_recorder: Any | None,
    ) -> None:
        self.events.put(("recording", microphone_recorder is not None))
        last_flush = time.monotonic()
        while not self.stop_event.is_set():
            system_data = system_recorder.record(numframes=BLOCK_SIZE)
            if microphone_recorder is None:
                output_data = system_data
            else:
                microphone_data, _overflowed = microphone_recorder.read(BLOCK_SIZE)
                # A mono microphone column broadcasts across the system channels.
                # Halving both sources leaves headroom and prevents hard clipping.
                output_data = np.clip(
                    0.5 * system_data + 0.5 * microphone_data,
                    -1.0,
                    1.0,
                )
            if len(output_data):
                wav_file.write(output_data)
                self.events.put(("waveform", waveform_payload(output_data)))
            if time.monotonic() - last_flush >= 2:
                wav_file.flush()
                last_flush = time.monotonic()


class AudioMonitorWorker:
    """Continuously previews an input without saving it."""

    def __init__(self, events: queue.Queue[tuple[str, Any]]) -> None:
        self.events = events
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(
        self,
        device: PlaybackDevice,
        microphone: PlaybackDevice | None = None,
    ) -> None:
        if self.running:
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self._capture,
            args=(device, microphone),
            daemon=True,
            name="audio-preview",
        )
        self.thread.start()

    def stop_and_wait(self, timeout: float = 2.0) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def _capture(
        self,
        device: PlaybackDevice,
        microphone: PlaybackDevice | None,
    ) -> None:
        try:
            loopback = sc.get_microphone(device.device_id, include_loopback=True)
            if loopback is None:
                raise RuntimeError("Windows could not preview this playback device.")
            with loopback.recorder(
                samplerate=SAMPLE_RATE,
                channels=device.channels,
                blocksize=BLOCK_SIZE,
            ) as system_recorder:
                if microphone is None:
                    self._monitor_blocks(system_recorder, None)
                else:
                    with sd.InputStream(
                        device=int(microphone.device_id),
                        samplerate=SAMPLE_RATE,
                        channels=1,
                        blocksize=BLOCK_SIZE,
                        dtype="float32",
                    ) as microphone_recorder:
                        self._monitor_blocks(system_recorder, microphone_recorder)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.events.put(("monitor_error", exc))

    def _monitor_blocks(self, system_recorder: Any, microphone_recorder: Any | None) -> None:
        self.events.put(("monitoring", microphone_recorder is not None))
        while not self.stop_event.is_set():
            system_data = system_recorder.record(numframes=BLOCK_SIZE)
            if microphone_recorder is None:
                preview_data = system_data
            else:
                microphone_data, _overflowed = microphone_recorder.read(BLOCK_SIZE)
                preview_data = np.clip(
                    0.5 * system_data + 0.5 * microphone_data,
                    -1.0,
                    1.0,
                )
            self.events.put(("waveform", waveform_payload(preview_data)))


class RecorderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("660x650")
        self.root.minsize(610, 620)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker = RecorderWorker(self.events)
        self.monitor = AudioMonitorWorker(self.events)
        self.devices: list[PlaybackDevice] = []
        self.microphones: list[PlaybackDevice] = []
        self.started_at: float | None = None
        self.closing = False
        self.refreshing_devices = False
        self.last_waveform = np.zeros(80, dtype=np.float32)

        self.device_var = tk.StringVar()
        self.include_microphone_var = tk.BooleanVar(value=False)
        self.microphone_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(application_dir() / "recordings"))
        self.status_var = tk.StringVar(value="Ready")
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.meter_var = tk.StringVar(value="Listening for system audio…")

        self._build_ui()
        self.refresh_devices()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._poll_events)
        self.root.after(250, self._tick_clock)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text=APP_TITLE, font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        ttk.Label(
            frame,
            text="Records everything played through the selected Windows output device.",
        ).grid(row=1, column=0, sticky="w", pady=(0, 18))

        ttk.Label(frame, text="Playback device").grid(row=2, column=0, sticky="w")
        device_row = ttk.Frame(frame)
        device_row.grid(row=3, column=0, sticky="ew", pady=(5, 14))
        device_row.columnconfigure(0, weight=1)
        self.device_box = ttk.Combobox(
            device_row, textvariable=self.device_var, state="readonly"
        )
        self.device_box.grid(row=0, column=0, sticky="ew")
        self.device_box.bind("<<ComboboxSelected>>", self._on_source_changed)
        self.refresh_button = ttk.Button(device_row, text="Refresh", command=self.refresh_devices)
        self.refresh_button.grid(row=0, column=1, padx=(8, 0))

        self.include_microphone_check = ttk.Checkbutton(
            frame,
            text="Include my microphone in the same recording",
            variable=self.include_microphone_var,
            command=self.on_microphone_toggle,
        )
        self.include_microphone_check.grid(row=4, column=0, sticky="w", pady=(0, 5))
        self.microphone_box = ttk.Combobox(
            frame,
            textvariable=self.microphone_var,
            state="disabled",
        )
        self.microphone_box.grid(row=5, column=0, sticky="ew", pady=(0, 14))
        self.microphone_box.bind("<<ComboboxSelected>>", self._on_source_changed)

        preview = ttk.LabelFrame(frame, text="Live audio input", padding=8)
        preview.grid(row=6, column=0, sticky="ew", pady=(0, 14))
        preview.columnconfigure(0, weight=1)
        self.waveform_canvas = tk.Canvas(
            preview,
            height=82,
            background="#111827",
            highlightthickness=0,
        )
        self.waveform_canvas.grid(row=0, column=0, sticky="ew")
        self.waveform_canvas.bind(
            "<Configure>", lambda _event: self._draw_waveform(self.last_waveform)
        )
        ttk.Label(preview, textvariable=self.meter_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )

        ttk.Label(frame, text="Save recordings in").grid(row=7, column=0, sticky="w")
        folder_row = ttk.Frame(frame)
        folder_row.grid(row=8, column=0, sticky="ew", pady=(5, 18))
        folder_row.columnconfigure(0, weight=1)
        self.output_entry = ttk.Entry(folder_row, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(folder_row, text="Browse…", command=self.choose_folder)
        self.browse_button.grid(row=0, column=1, padx=(8, 0))

        status = ttk.LabelFrame(frame, text="Status", padding=12)
        status.grid(row=9, column=0, sticky="ew", pady=(0, 18))
        status.columnconfigure(0, weight=1)
        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        ttk.Label(status, textvariable=self.elapsed_var, font=("Consolas", 16, "bold")).grid(
            row=0, column=1, sticky="e"
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=10, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self.start_button = ttk.Button(buttons, text="Start recording", command=self.start_recording)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=6)
        self.stop_button = ttk.Button(
            buttons, text="Stop and save", command=self.stop_recording, state="disabled"
        )
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(6, 0), ipady=6)

        ttk.Label(
            frame,
            text=(
                "Tip: use headphones when including your microphone to avoid echo. "
                "Use your meeting app’s consent indicator and recording policy."
            ),
            foreground="#555555",
            wraplength=610,
        ).grid(row=11, column=0, sticky="w", pady=(18, 0))

    def refresh_devices(self) -> None:
        self.refreshing_devices = True
        self.monitor.stop_and_wait()
        try:
            selected_id = None
            current_index = self.device_box.current()
            if 0 <= current_index < len(self.devices):
                selected_id = self.devices[current_index].device_id

            selected_microphone_id = None
            current_microphone_index = self.microphone_box.current()
            if 0 <= current_microphone_index < len(self.microphones):
                selected_microphone_id = self.microphones[current_microphone_index].device_id

            self.devices = available_playback_devices()
            self.device_box["values"] = [d.name for d in self.devices]
            preferred_id = selected_id or default_device_id()
            preferred_index = next(
                (i for i, d in enumerate(self.devices) if d.device_id == preferred_id), 0
            )
            if self.devices:
                self.device_box.current(preferred_index)
                self.status_var.set("Ready")
                self.start_button.configure(state="normal")
            else:
                self.device_var.set("")
                self.status_var.set("No playback device found")
                self.start_button.configure(state="disabled")

            self.microphones = available_microphones()
            self.microphone_box["values"] = [d.name for d in self.microphones]
            preferred_microphone_id = selected_microphone_id or default_microphone_id()
            preferred_microphone_index = next(
                (
                    i
                    for i, microphone in enumerate(self.microphones)
                    if microphone.device_id == preferred_microphone_id
                ),
                0,
            )
            if self.microphones:
                self.microphone_box.current(preferred_microphone_index)
                self.include_microphone_check.configure(state="normal")
            else:
                self.microphone_var.set("")
                self.include_microphone_var.set(False)
                self.include_microphone_check.configure(state="disabled")
            self.on_microphone_toggle()
        except Exception as exc:
            self.status_var.set("Could not list playback devices")
            self.start_button.configure(state="disabled")
            messagebox.showerror(APP_TITLE, f"Could not list playback devices:\n\n{exc}")
        finally:
            self.refreshing_devices = False
            if not self.worker.running and not self.closing:
                self._start_monitor()

    def on_microphone_toggle(self) -> None:
        state = (
            "readonly"
            if self.include_microphone_var.get() and self.microphones and not self.worker.running
            else "disabled"
        )
        self.microphone_box.configure(state=state)
        if not self.refreshing_devices:
            self._restart_monitor()

    def _on_source_changed(self, _event: tk.Event | None = None) -> None:
        self._restart_monitor()

    def _selected_sources(self) -> tuple[PlaybackDevice | None, PlaybackDevice | None]:
        device_index = self.device_box.current()
        device = self.devices[device_index] if 0 <= device_index < len(self.devices) else None
        microphone = None
        if self.include_microphone_var.get():
            microphone_index = self.microphone_box.current()
            if 0 <= microphone_index < len(self.microphones):
                microphone = self.microphones[microphone_index]
        return device, microphone

    def _start_monitor(self) -> None:
        if self.monitor.running or self.worker.running or self.closing:
            return
        device, microphone = self._selected_sources()
        if device is not None:
            self.meter_var.set("Listening for audio…")
            self.monitor.start(device, microphone)

    def _restart_monitor(self) -> None:
        if self.refreshing_devices or self.worker.running or self.closing:
            return
        self.monitor.stop_and_wait()
        self._start_monitor()

    def choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_var.get())
        if chosen:
            self.output_var.set(chosen)

    def start_recording(self) -> None:
        index = self.device_box.current()
        if not (0 <= index < len(self.devices)):
            messagebox.showerror(APP_TITLE, "Select a playback device first.")
            return

        microphone = None
        if self.include_microphone_var.get():
            microphone_index = self.microphone_box.current()
            if not (0 <= microphone_index < len(self.microphones)):
                messagebox.showerror(APP_TITLE, "Select a microphone first.")
                return
            microphone = self.microphones[microphone_index]

        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showerror(APP_TITLE, "Choose an output folder first.")
            return

        try:
            output_dir = Path(output_text).expanduser().resolve()
            self.monitor.stop_and_wait()
            planned_path = self.worker.start(self.devices[index], output_dir, microphone)
        except Exception as exc:
            self._start_monitor()
            messagebox.showerror(APP_TITLE, f"Could not start recording:\n\n{exc}")
            return

        self.started_at = time.monotonic()
        self.status_var.set(f"Starting: {planned_path.name}")
        self._set_controls(recording=True)

    def stop_recording(self) -> None:
        if self.worker.running:
            self.status_var.set("Stopping and finalizing WAV file…")
            self.stop_button.configure(state="disabled")
            self.worker.stop()

    def _set_controls(self, recording: bool) -> None:
        self.start_button.configure(state="disabled" if recording else "normal")
        self.stop_button.configure(state="normal" if recording else "disabled")
        self.device_box.configure(state="disabled" if recording else "readonly")
        self.refresh_button.configure(state="disabled" if recording else "normal")
        self.include_microphone_check.configure(
            state="disabled" if recording or not self.microphones else "normal"
        )
        self.microphone_box.configure(
            state=(
                "disabled"
                if recording or not self.include_microphone_var.get() or not self.microphones
                else "readonly"
            )
        )
        self.output_entry.configure(state="disabled" if recording else "normal")
        self.browse_button.configure(state="disabled" if recording else "normal")

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "recording":
                    mode = "system + microphone" if payload else "system audio"
                    self.status_var.set(f"Recording {mode}")
                elif event == "stopped":
                    final_path, duration = payload
                    self.started_at = None
                    self.elapsed_var.set(self._format_duration(duration))
                    self.status_var.set(f"Saved: {Path(final_path).name}")
                    self._set_controls(recording=False)
                    if self.closing:
                        self.root.destroy()
                        return
                    else:
                        self._start_monitor()
                        messagebox.showinfo(APP_TITLE, f"Recording saved to:\n\n{final_path}")
                elif event == "error":
                    exc, partial_path = payload
                    self.started_at = None
                    self._set_controls(recording=False)
                    self.status_var.set("Recording failed")
                    recovery = (
                        f"\n\nA partial recording may be recoverable at:\n{partial_path}"
                        if partial_path
                        else ""
                    )
                    messagebox.showerror(APP_TITLE, f"Recording failed:\n\n{exc}{recovery}")
                    if self.closing:
                        self.root.destroy()
                        return
                    else:
                        self._start_monitor()
                elif event == "waveform":
                    self._draw_waveform(payload)
                elif event == "monitoring":
                    source = "system + microphone" if payload else "system audio"
                    self.meter_var.set(f"Listening to {source} — waiting for sound")
                elif event == "monitor_error":
                    self.meter_var.set(f"Audio preview unavailable: {payload}")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_events)

    def _tick_clock(self) -> None:
        if self.started_at is not None:
            self.elapsed_var.set(self._format_duration(time.monotonic() - self.started_at))
        if self.root.winfo_exists():
            self.root.after(250, self._tick_clock)

    def _draw_waveform(self, samples: np.ndarray) -> None:
        self.last_waveform = np.asarray(samples, dtype=np.float32)
        width = max(2, self.waveform_canvas.winfo_width())
        height = max(2, self.waveform_canvas.winfo_height())
        center = height / 2
        peak = float(np.max(np.abs(self.last_waveform))) if self.last_waveform.size else 0.0
        scale = (height * 0.42) / max(peak, 0.02)
        x_values = np.linspace(0, width - 1, len(self.last_waveform))
        coordinates: list[float] = []
        for x_value, sample in zip(x_values, self.last_waveform):
            coordinates.extend((float(x_value), center - float(sample) * scale))

        detected = peak >= 0.001
        color = "#22c55e" if detected else "#64748b"
        self.waveform_canvas.delete("all")
        self.waveform_canvas.create_line(0, center, width, center, fill="#334155")
        if len(coordinates) >= 4:
            self.waveform_canvas.create_line(*coordinates, fill=color, width=2, smooth=True)
        mode = "Recording" if self.worker.running else "Listening"
        self.meter_var.set(
            f"{mode} — sound detected" if detected else f"{mode} — waiting for sound"
        )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def on_close(self) -> None:
        if self.worker.running:
            if not messagebox.askyesno(
                APP_TITLE, "A recording is in progress. Stop, save, and close?"
            ):
                return
            self.closing = True
            self.status_var.set("Stopping and finalizing WAV file…")
            self._set_controls(recording=True)
            self.stop_button.configure(state="disabled")
            self.worker.stop()
        else:
            self.closing = True
            self.monitor.stop_and_wait()
            self.root.destroy()


def self_test() -> int:
    devices = available_playback_devices()
    microphones = available_microphones()
    print(f"Found {len(devices)} playback device(s).")
    for index, device in enumerate(devices, start=1):
        print(f"{index}. {device.name} [{device.channels} channel(s)]")
    if not devices:
        print("ERROR: Windows did not report any playback devices.", file=sys.stderr)
        return 1
    print(f"Found {len(microphones)} WASAPI microphone(s).")
    for index, microphone in enumerate(microphones, start=1):
        print(f"{index}. {microphone.name}")
    print("Recorder dependencies and device discovery are working.")
    return 0


def timed_recording(seconds: float, output_dir: Path) -> int:
    """Internal packaged-build smoke test used by the delivery verification."""
    try:
        devices = available_playback_devices()
        if not devices or seconds <= 0:
            print("Timed test has no playback device or an invalid duration.", file=sys.stderr)
            return 1
        selected_id = default_device_id()
        device = next((item for item in devices if item.device_id == selected_id), devices[0])
        events: queue.Queue[tuple[str, Any]] = queue.Queue()
        worker = RecorderWorker(events)
        worker.start(device, output_dir)
        event, payload = events.get(timeout=15)
        if event != "recording":
            print(f"Timed test could not start: {event}: {payload!r}", file=sys.stderr)
            return 1
        time.sleep(seconds)
        worker.stop()
        if worker.thread:
            worker.thread.join(timeout=15)
        if worker.running:
            print("Timed test capture thread did not stop.", file=sys.stderr)
            return 1
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event, payload = events.get(timeout=max(0.01, deadline - time.monotonic()))
            if event == "stopped":
                return 0
            if event == "error":
                print(f"Timed test could not finalize: {event}: {payload!r}", file=sys.stderr)
                return 1
            # Waveform events are expected while recording; keep draining until
            # the worker's terminal stopped/error event arrives.
        print("Timed test did not receive a finalization event.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Timed test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify dependencies and list playback devices without recording",
    )
    parser.add_argument("--record-seconds", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.record_seconds is not None:
        output_dir = (args.output_dir or (application_dir() / "recordings")).resolve()
        return timed_recording(args.record_seconds, output_dir)

    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
