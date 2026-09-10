# Meeting System Audio Recorder

A small Windows tool that records the sound played through a selected output device. It uses Windows WASAPI loopback, so **Stereo Mix is not required**.

The application is local-only: it does not upload audio or require an account.

## Start it

1. Double-click **MeetingSystemAudioRecorder.exe** (or **Start Recorder.bat**).
2. Select the playback device that your meeting app is using.
3. Play a little audio and confirm the live waveform moves and says **sound detected**.
4. Optionally select **Include my microphone** and choose your microphone. Headphones are recommended to prevent echo.
5. Click **Start recording**.
6. Click **Stop and save** when the meeting ends.

Recordings are saved as timestamped, 48 kHz, 16-bit WAV files in the `recordings` folder by default. You can choose a different folder in the app.

## Important behavior

- It records system playback: other participants, shared videos, notification sounds, and anything else sent to the chosen device.
- By default it records only system audio. Turn on **Include my microphone** to mix your voice into the same WAV file.
- The live waveform listens whenever the app is open—even before recording—so you can verify the selected source. Preview audio is discarded and never saved.
- If you switch headphones or speakers during a meeting, stop the recording, select the new device, and start again.
- If the app or computer crashes, look for a file ending in `.recording.wav`; it may contain recoverable audio.
- Obtain consent and follow the applicable meeting, employer, and local recording rules.

## Troubleshooting

- **The file is silent:** make sure the selected playback device matches the output device shown in the meeting app and Windows Volume Mixer.
- **No device appears:** connect or enable the output device, then click **Refresh**.
- **Bluetooth headset:** Windows may expose separate playback endpoints. Select the one the meeting app is actively using.
- **Executable will not open:** `Start Recorder.bat` normally opens the packaged app too. For source-mode diagnostics, run **Test Setup.bat**; it creates an isolated Python environment and lists the detected devices.

No audio is uploaded. Recording and saving happen locally on this computer.

## Run from source

Python 3.10 or newer is recommended.

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

The build script uses an isolated `.build-venv` environment and PyInstaller. It writes `MeetingSystemAudioRecorder.exe` to the repository root.

## Privacy and repository safety

The repository ignores `recordings/`, all WAV files, virtual environments, and build caches. Never commit meeting recordings to a public repository. Obtain consent and follow the laws and organizational policies that apply to your meeting.

## License

[MIT](LICENSE)
