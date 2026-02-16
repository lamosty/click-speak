<p align="center">
  <h1 align="center">ClickSpeak</h1>
  <p align="center">Keyboard-free voice-to-text for macOS.<br>Point. Click. Speak. Done.</p>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-macOS-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/apple%20silicon-M1%2B-black.svg" alt="Apple Silicon">
  <img src="https://img.shields.io/badge/python-3.12-3776AB.svg" alt="Python">
</p>

---

ClickSpeak is a macOS menu bar app that turns your Mac into a hands-free dictation machine. It runs speech recognition **100% locally** on Apple Silicon — no cloud, no subscription, no data leaving your Mac.

Middle-click to record, middle-click to stop, text gets typed automatically. No keyboard required, ever.

## Why ClickSpeak

Tools like SuperWhisper ($10/mo) charge a subscription for open-source models and break constantly with Bluetooth audio. ClickSpeak fixes that:

| Problem | ClickSpeak |
|---------|------------|
| Bluetooth headphones disconnect and app panics | Waits patiently, auto-reconnects in ~1.5s — survives sleep/wake, case in/out, whatever |
| $10/month for open-source speech models | Free. Same NVIDIA Parakeet model, runs locally via MLX |
| Still need a keyboard to trigger dictation | Middle mouse click to record. Text auto-types with Enter. Zero keyboard |
| Forgot to click the window? Transcription lost | Auto-clicks the window under your cursor before pasting — text always lands where you're pointing |

## Features

### Mouse-only workflow
- **Middle click** to start/stop recording — no keyboard shortcut needed
- **Middle click + drag** for fast scrolling (configurable multiplier)
- **Scroll wheel multiplier** for discrete mouse wheels (trackpad unaffected)
- **Auto-focuses the window under your cursor** before pasting — no need to click first, text always lands in the right place
- Transcribed text is **auto-typed and Enter is pressed** — point at any text field and speak

### Bluetooth headphone persistence
- Disconnect headphones — app waits, auto-reconnects when they're back
- Survives sleep/wake cycles, charging case in/out, range dropouts
- **Never falls back to the MacBook microphone** — uses your configured device or waits
- CoreAudio callback for instant detection (~1.5s), with polling fallback

### Local transcription
- **NVIDIA Parakeet TDT 0.6B** on Apple Silicon via MLX — ~110x realtime speed
- 10-second recording transcribes in ~90ms
- **Wake word** via openWakeWord — configurable ("Hey Jarvis", "Alexa", "Hey Mycroft"), <1% CPU while listening
- **Silero VAD** for automatic end-of-speech detection — no manual stop needed

### Bluetooth audio intelligence
- **On-demand mic** (default) — audio stream opens only while recording, closes immediately after. No always-on microphone, no interference with other apps' keyboard shortcuts
- **Pause audio while recording** — optional toggle pauses music before recording, resumes after transcription. Avoids the degraded HFP audio entirely
- **Volume boost** — when music keeps playing during recording, volume automatically increases +15% to compensate for BT HFP quality drop, restores when done

### Quality of life
- **Transcription history** — last 10 transcriptions in the menu bar, click to copy
- **Three trigger modes** — on-demand (middle click / Option+Space), wake word, or both
- **Audio device picker** — select input device from the menu bar
- **Shortcuts reference** — menu bar shows all active shortcuts
- **Sound feedback** — Glass on start, Funk on stop
- **Menu bar icons** — SF Symbols show current state (idle/listening/recording/transcribing)
- **No event taps** — uses NSEvent global monitors instead of CGEventTaps, so other apps' keyboard shortcuts (ShiftIt, BetterTouchTool, etc.) keep working

## Requirements

- macOS on Apple Silicon (M1 or later)
- Python 3.12
- A mouse with a middle button (for the full workflow — hotkey mode works without one)

## Install

```bash
git clone https://github.com/lamosty/clickspeak.git
cd clickspeak
bash scripts/install_app.sh
```

The install script:
1. Creates an isolated Python 3.12 runtime at `~/Library/Application Support/ClickSpeak/runtime`
2. Installs all dependencies into that runtime
3. Compiles a native Mach-O launcher (so macOS grants permissions to "ClickSpeak", not "python3")
4. Installs to `/Applications/ClickSpeak.app`

Launch from **Spotlight** or the Applications folder.

### First launch

On first launch, ClickSpeak walks you through granting permissions one at a time:

1. **Input Monitoring** — for keyboard shortcuts and mouse events
2. **Accessibility** — for typing transcribed text into other apps
3. **Microphone** — requested automatically when the audio stream starts

### Updating

After pulling new changes, just re-run the install script. It only recompiles the native launcher when `src/launcher.c` changes — Python code updates are picked up on next app restart.

```bash
git pull
bash scripts/install_app.sh
```

## Usage

### Middle mouse workflow (recommended)

1. Point your cursor at any text field
2. **Middle-click** to start recording (Glass sound plays)
3. Speak your text
4. **Middle-click** to stop (Funk sound plays), or say the wake word, or just pause and let VAD detect the silence
5. ClickSpeak clicks the window under your cursor to focus it, pastes the text, and presses Enter

### Wake word

Say **"Hey Jarvis"** to start recording. Say it again to stop. The wake word is stripped from the transcription automatically.

### Hotkey

Press **Option+Space** to toggle recording. Works alongside wake word when trigger mode is set to "both".

### Fast scrolling

Hold **middle mouse + drag** up/down for accelerated scrolling. The scroll multiplier is configurable. Release without dragging to trigger dictation instead.

## Configuration

ClickSpeak is opinionated and works out of the box. There is no settings UI.

Config lives at `~/.config/clickspeak/config.json`:

| Key | Default | Description |
|-----|---------|-------------|
| `audio_device` | `null` | Input device name, or `null` for system default |
| `trigger_mode` | `"on_demand"` | `"on_demand"`, `"wake_word"`, or `"both"` |
| `pause_audio_while_recording` | `false` | Pause/resume music around recordings |
| `wake_word` | `"hey_jarvis"` | OpenWakeWord model name |
| `wake_word_threshold` | `0.5` | Detection confidence (0.0–1.0) |
| `silence_threshold` | `1.5` | Seconds of silence before VAD stops recording |
| `model_name` | `"mlx-community/parakeet-tdt-0.6b-v3"` | HuggingFace model for transcription |
| `middle_mouse_fast_scroll_enabled` | `true` | Enable middle-click drag scrolling |
| `middle_mouse_drag_scroll_multiplier` | `12.0` | Scroll speed during middle-click drag |
| `mouse_wheel_scroll_multiplier` | `1.0` | Discrete scroll wheel speed multiplier |

The intended way to deeply customize is to fork the source — it's ~1,200 lines of Python across 8 files.

## Architecture

```
src/clickspeak/
├── main.py          # Menu bar app, event handling, permissions, UI
├── audio.py         # Audio capture, resampling, device management
├── transcribe.py    # Speech-to-text via Parakeet MLX
├── wake_word.py     # Wake word detection via openWakeWord
├── vad.py           # Voice activity detection via Silero VAD
├── inject.py        # Text injection (clipboard + Cmd+V + Return)
├── config.py        # Config loading/saving
└── __main__.py      # CLI entrypoint (--check-permissions, --version)

src/launcher.c       # Native Mach-O launcher for macOS permission tracking
scripts/
└── install_app.sh   # Build and install to /Applications
```

**Why a native launcher?** macOS grants permissions (Accessibility, Input Monitoring, Microphone) based on the code signature of the binary. A Python script appears as "python3" to macOS. The native C launcher embeds the Python interpreter so macOS sees "ClickSpeak" — permissions stick across restarts.

## Models

All models run locally. Nothing is sent to the cloud.

| Component | Model | Size | License |
|-----------|-------|------|---------|
| Speech-to-text | [Parakeet TDT 0.6B](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3) | 600M params | Apache 2.0 |
| Wake word | [openWakeWord](https://github.com/dscripka/openWakeWord) | ~2MB | Apache 2.0 |
| Voice activity | [Silero VAD v5](https://github.com/snakers4/silero-vad) | ~2MB | MIT |

Models are downloaded automatically on first use from HuggingFace.

## Logs

```bash
# Live log output
tail -f ~/.config/clickspeak/app.log
```

## License

[MIT](LICENSE)

Copyright 2025 Rastislav Lamoš
