# Meeting System Audio Recorder

A small Windows tool that records the sound played through a selected output device. It uses Windows WASAPI loopback, so **Stereo Mix is not required**.

The application is local-only: it does not upload audio or require an account.

## Start it

1. Double-click **MeetingSystemAudioRecorder.exe** (or **Start Recorder.bat**).
2. Under **Sources**, select the playback device that your meeting app is using.
3. Play a little audio and confirm that the live scope moves and the green meter beside the device responds.
4. Optionally tick **Microphone** and choose your microphone. Its own meter shows that it is being heard. Headphones are recommended to prevent echo.
5. Click the round red button (or press **Ctrl+R**). The timer shows how much audio has been written.
6. Click the button again (now a stop square) when the meeting ends. The saved file name appears under the scope, with **Show in folder**.

Recordings are saved as timestamped, 48 kHz, 16-bit stereo WAV files. By default they go to the `recordings` folder next to the app, or to `Music\Meeting Recordings` if that folder cannot be written (for example under `Program Files`). The app remembers your devices and folder between runs.

## Important behavior

- It records the selected endpoint's shared-mode Windows mix: other participants, shared videos, notification sounds, and anything else played there. Audio that an app or driver protects from capture, and audio played in exclusive mode, is not recorded.
- By default it records only system audio. Tick **Microphone** to mix your voice into the same WAV file.
- The live preview listens whenever the app is open—even before recording—so you can verify the selected source. Preview audio is discarded. Starting a recording reuses the open capture instead of reopening the devices. Start and Stop act on 100 ms blocks, so the first block saved may include up to 0.1 s from just before you clicked.
- When the microphone is included, the app holds at most 0.15 s of microphone audio waiting to be mixed. If the two devices' clocks drift, it drops or pads a few milliseconds of microphone audio rather than letting the delay grow. If the computer stalls the app for a moment, the two sources can briefly be further apart.
- If the microphone stops sending audio (for example, it is unplugged), a notice appears and the recording continues with system audio only. The notice clears if the microphone comes back.
- If you switch headphones or speakers during a meeting, stop the recording, select the new device, and start again.
- Very long recordings are split into parts (`_part2`, `_part3`, …) before the 4 GB WAV limit, about every 6 hours.
- While recording, audio goes to a file ending in `.recording.wav`, whose header is updated after every write. If the app crashes, that file normally stays playable up to the last completed write; after a power loss, the most recent seconds may be missing. If a finished file cannot be renamed (for example, because another program has it open), the app says so and the audio stays in the `.recording.wav` file.
- Save recordings to a local disk. A slow network folder can stall capture.
- Obtain consent and follow the applicable meeting, employer, and local recording rules.

## Troubleshooting

- **The file is silent:** make sure the selected playback device matches the output device shown in the meeting app and Windows Volume Mixer.
- **No device appears:** connect or enable the device, then click **Refresh devices**. Newly plugged-in microphones also appear after a refresh.
- **Bluetooth headset:** Windows may expose separate playback endpoints. Select the one the meeting app is actively using.
- **Executable will not open:** `Start Recorder.bat` normally opens the packaged app too. For source-mode diagnostics, run **Test Setup.bat**; it creates an isolated Python environment, lists the detected devices, and reports whether the check passed.

No audio is uploaded. Recording and saving happen locally on this computer.

## Run from source

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe recorder.py
```

## Build the Windows executable

From PowerShell:

```powershell
.\build.ps1
```

The build script uses an isolated `.build-venv` environment and PyInstaller. It builds into `.build\dist`, runs a two-second recording smoke test with the new executable, and only then replaces `MeetingSystemAudioRecorder.exe` in the repository root. Any failed step stops the build.

## Privacy and repository safety

The repository ignores `recordings/`, all WAV files, virtual environments, and build caches. Never commit meeting recordings to a public repository. Obtain consent and follow the laws and organizational policies that apply to your meeting.

## License

[MIT](LICENSE)
