# Mic Soundboard — Steam Virtual Mic Injector

A streamlined, zero-driver-install Windows 11 soundboard & real-time microphone audio injector for **Discord** and **in-game voice chat** (using your existing **Steam Streaming Microphone** loopback).

## Features
- **Zero Windows Driver Installs**: Mixes your physical microphone with soundboard clips and routes them to `Speakers (Steam Streaming Microphone)` with automatic `-30.0 dB` gain compensation.
- **Auto-Switch Windows Default Mic**: Automatically switches Windows Default Recording & Communication Device to `Microphone (Steam Streaming Microphone)` while running, and restores your original hardware mic on exit.
- **Universal Media Support**: Drag & drop `.mp3`, `.wav`, `.ogg`, `.flac`, `.m4a`, `.mp4`, `.webm`, or `.opus` files (decoded via PyAV — no external `ffmpeg.exe` required).
- **YouTube & YouTube Music Import**: Paste single track or playlist links directly into the URL bar (`🔗 Import & Play`).
- **Click-to-Stop / `Shift+Click` to Stack**: Click any playing pad to stop it, or `Shift+Click` to stack/retrigger.
- **Auto-Sidechain ("Duck Music When I Speak")**: Smoothly dips background music/clips whenever you speak into your mic.
- **Non-Destructive Clip Trimmer**: Set Start/End timestamps in seconds per pad.
- **Global In-Game Hotkeys**: Bind keys (`F1`–`F11`, `Numpad`, combos) to pads, plus `F12` to Stop All.

## Quick Start
- **Standalone EXE**: Run `dist/Mic_Soundboard.exe` (build via `python build_exe.py`).
- **From Source**: Double-click `START_SOUNDBOARD.bat`.

## Testing
```powershell
$env:PYTHONPATH="."
.\.venv\Scripts\python.exe -m pytest tests -v
```
