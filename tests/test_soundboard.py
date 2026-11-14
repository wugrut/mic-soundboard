"""Tests for Mic Soundboard audio engine, decoder, and FastAPI server."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from app.audio_decoder import TARGET_SAMPLE_RATE, decode_audio_file, resample_linear
from app.audio_engine import AudioEngine, RingBuffer
from app.config_store import ConfigStore
from app.server import create_app


def _make_wav(path: Path, sr: int = 44100, channels: int = 1, duration: float = 0.25) -> None:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False, dtype=np.float32)
    tone = 0.5 * np.sin(2 * np.pi * 440 * t)
    data = tone if channels == 1 else np.column_stack([tone, tone * 0.5])
    sf.write(str(path), data, sr)


def test_ring_buffer_wrap_and_overflow():
    rb = RingBuffer(capacity_frames=8)
    a = np.ones((5, 2), dtype=np.float32)
    rb.write(a)
    assert rb.available == 5
    out = rb.read(3)
    assert out.shape == (3, 2)
    assert np.allclose(out, 1.0)
    assert rb.available == 2

    # Write 7 more frames -> total 9 > capacity 8 -> oldest 1 dropped
    b = np.full((7, 2), 2.0, dtype=np.float32)
    rb.write(b)
    assert rb.available == 8
    out2 = rb.read(8)
    assert np.allclose(out2[0], 1.0)
    assert np.allclose(out2[1:], 2.0)


def test_decode_audio_file_resamples_to_48k_stereo(tmp_path: Path):
    wav_path = tmp_path / "mono44.wav"
    _make_wav(wav_path, sr=44100, channels=1, duration=0.2)
    pcm, sr = decode_audio_file(wav_path)
    assert sr == TARGET_SAMPLE_RATE
    assert pcm.ndim == 2 and pcm.shape[1] == 2
    assert pcm.dtype == np.float32
    assert abs(len(pcm) - int(48000 * 0.2)) < 1500


def test_resample_linear():
    src = np.ones((480, 2), dtype=np.float32)
    dst = resample_linear(src, 48000, 44100)
    assert dst.shape == (441, 2)
    assert np.allclose(dst, 1.0)


def test_audio_engine_mix_and_steam_gain_compensation(tmp_path: Path):
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    _make_wav(sounds_dir / "airhorn.wav", sr=48000, channels=2, duration=0.2)

    store = ConfigStore(tmp_path / "config.json")
    engine = AudioEngine(store, sounds_dir)
    engine.sync_sounds_directory()
    assert "airhorn" in store.config.pads

    # Simulate Steam Streaming Microphone output device
    from app.audio_engine import AudioDeviceInfo

    engine.active_vout_device = AudioDeviceInfo(
        index=19,
        name="Speakers (Steam Streaming Microphone)",
        hostapi="Windows WASAPI",
        max_inputs=0,
        max_outputs=2,
        default_samplerate=48000,
        is_virtual=True,
    )
    assert engine.get_effective_vout_gain_db() == -30.0

    # Push mic signal + play clip
    engine.mic_ring.write(np.full((480, 2), 0.1, dtype=np.float32))
    engine.play_pad("airhorn")
    out = np.zeros((480, 2), dtype=np.float32)
    engine._on_virtual_output(out, 480, None, None)

    # Output should be scaled by -30dB (~0.0316x), so peak must be well below 0.05
    assert 0.0 < float(np.max(np.abs(out))) < 0.05
    assert engine.sb_level > 0.0

    engine.stop_all()
    assert engine.get_playing_status() == {}


def test_server_api_lifecycle(tmp_path: Path):
    app = create_app(base_dir=tmp_path, start_audio=False)
    client = TestClient(app)

    res = client.get("/")
    assert res.status_code == 200

    state = client.get("/api/state").json()
    assert state["pads"] == []
    assert "recommended_discord_mic" in state

    wav_path = tmp_path / "test_clip.wav"
    _make_wav(wav_path, sr=48000, channels=2, duration=0.15)
    with wav_path.open("rb") as f:
        up = client.post("/api/pads/upload", files={"file": ("test_clip.wav", f, "audio/wav")})
    assert up.status_code == 200
    pad_id = up.json()["pad"]["id"]

    play_res = client.post(f"/api/pads/{pad_id}/play")
    assert play_res.status_code == 200
    assert play_res.json()["playing"] == pad_id

    patch_res = client.patch(f"/api/pads/{pad_id}", json={"name": "Airhorn", "hotkey": "f1", "loop": True})
    assert patch_res.status_code == 200
    assert patch_res.json()["pad"]["hotkey"] == "f1"

    set_res = client.post("/api/settings", json={"mic_muted": True, "virtual_out_gain_db": -28.0})
    assert set_res.status_code == 200
    assert set_res.json()["effective_vout_gain_db"] == -28.0

    stop_res = client.post("/api/stop_all")
    assert stop_res.status_code == 200

    del_res = client.delete(f"/api/pads/{pad_id}")
    assert del_res.status_code == 200
    assert client.get("/api/state").json()["pads"] == []


def test_youtube_import_endpoint(tmp_path: Path, monkeypatch):
    app = create_app(base_dir=tmp_path, start_audio=False)
    client = TestClient(app)

    def fake_import(url: str, sounds_dir: Path):
        out = sounds_dir / "Synthwave Mix.wav"
        _make_wav(out, sr=48000, channels=2, duration=0.1)
        return [(out, "Synthwave Mix")]

    monkeypatch.setattr("app.server.import_youtube_audio", fake_import)
    res = client.post("/api/pads/youtube", json={"url": "https://music.youtube.com/watch?v=abc123", "auto_play": True})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["pad"]["name"] == "Synthwave Mix"


def test_toggle_trim_and_voice_ducking(tmp_path: Path):
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir()
    _make_wav(sounds_dir / "song.wav", sr=48000, channels=2, duration=1.0)

    store = ConfigStore(tmp_path / "config.json")
    engine = AudioEngine(store, sounds_dir)
    engine.sync_sounds_directory()

    pad = store.config.pads["song"]
    pad.start_sec = 0.2
    pad.end_sec = 0.5
    store.save()

    res1 = engine.play_pad("song")
    assert res1["playing"] == "song"
    assert abs(res1["duration_sec"] - 0.3) < 0.02

    # Clicking again without stack=True toggles it off
    res2 = engine.play_pad("song")
    assert res2 == {"stopped": "song"}

    # Voice ducking lowers _voice_duck_gain when mic_level > 0.08
    store.update(duck_music_on_voice=True)
    engine.mic_level = 0.5
    engine.play_pad("song")
    out = np.zeros((480, 2), dtype=np.float32)
    engine._on_virtual_output(out, 480, None, None)
    assert engine._voice_duck_gain < 1.0


def test_extra_sound_locations_persistent_scan(tmp_path: Path):
    base = tmp_path / "app"
    extra = tmp_path / "external_sounds"
    extra.mkdir(parents=True)
    ext_wav = extra / "air_raid.wav"
    _make_wav(ext_wav, sr=48000, channels=2, duration=0.1)

    app = create_app(base_dir=base, start_audio=False)
    client = TestClient(app)

    res = client.post("/api/settings", json={"sound_locations": [str(extra)]})
    assert res.status_code == 200

    state = client.get("/api/state").json()
    pad_ids = [p["id"] for p in state["pads"]]
    assert "air_raid" in pad_ids

    # Deleting the pad should not delete external user file outside sounds_dir
    del_res = client.delete("/api/pads/air_raid")
    assert del_res.status_code == 200
    assert ext_wav.exists()
