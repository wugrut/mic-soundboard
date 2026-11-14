"""Real-time low-latency audio mixer & virtual microphone router."""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd

from .audio_decoder import SUPPORTED_EXTENSIONS, TARGET_SAMPLE_RATE, decode_audio_file, resample_linear
from .config_store import ConfigStore, PadConfig

logger = logging.getLogger("mic_soundboard.engine")

VIRTUAL_OUT_KEYWORDS = ("steam streaming microphone", "cable input", "vb-audio", "voicemeeter")
VIRTUAL_EXCLUDE_KEYWORDS = ("steam streaming", "vb-audio", "cable ", "voicemeeter")
BLOCKSIZE = 480  # 10ms @ 48kHz


@dataclass
class AudioDeviceInfo:
    index: int
    name: str
    hostapi: str
    max_inputs: int
    max_outputs: int
    default_samplerate: int
    is_virtual: bool


@dataclass
class LoadedClip:
    pad_id: str
    pcm: np.ndarray  # float32 (N, 2) @ 48kHz
    duration_sec: float


@dataclass
class ActivePlayback:
    pad_id: str
    pcm: np.ndarray
    pos: int = 0
    volume: float = 1.0
    loop: bool = False


class RingBuffer:
    """Thread-safe stereo float32 ring buffer for audio stream handoff."""

    def __init__(self, capacity_frames: int = 16384):
        self.capacity = capacity_frames
        self.buf = np.zeros((capacity_frames, 2), dtype=np.float32)
        self.read_idx = 0
        self.write_idx = 0
        self.available = 0
        self.lock = threading.Lock()

    def write(self, data: np.ndarray) -> None:
        n = len(data)
        if n == 0:
            return
        with self.lock:
            if n >= self.capacity:
                data = data[-self.capacity :]
                n = len(data)
                self.read_idx = 0
                self.write_idx = 0
                self.available = 0

            overflow = (self.available + n) - self.capacity
            if overflow > 0:
                self.read_idx = (self.read_idx + overflow) % self.capacity
                self.available -= overflow

            first = min(n, self.capacity - self.write_idx)
            self.buf[self.write_idx : self.write_idx + first] = data[:first]
            rem = n - first
            if rem > 0:
                self.buf[0:rem] = data[first:]
            self.write_idx = (self.write_idx + n) % self.capacity
            self.available += n

    def read(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), dtype=np.float32)
        with self.lock:
            to_read = min(frames, self.available)
            if to_read == 0:
                return out
            first = min(to_read, self.capacity - self.read_idx)
            out[:first] = self.buf[self.read_idx : self.read_idx + first]
            rem = to_read - first
            if rem > 0:
                out[first:to_read] = self.buf[0:rem]
            self.read_idx = (self.read_idx + to_read) % self.capacity
            self.available -= to_read
        return out

    def clear(self) -> None:
        with self.lock:
            self.read_idx = 0
            self.write_idx = 0
            self.available = 0


def _is_virtual_name(name: str) -> bool:
    low = name.lower()
    return any(k in low for k in VIRTUAL_EXCLUDE_KEYWORDS)


class AudioEngine:
    """Manages audio devices, clip decoding, real-time mixing, and virtual mic output."""

    def __init__(self, store: ConfigStore, sounds_dir: Path):
        self.store = store
        self.sounds_dir = sounds_dir
        self.sounds_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self.clips: Dict[str, LoadedClip] = {}
        self.active_playbacks: List[ActivePlayback] = []

        self.mic_ring = RingBuffer(capacity_frames=16384)
        self.monitor_ring = RingBuffer(capacity_frames=16384)

        self._mic_stream: Optional[sd.InputStream] = None
        self._vout_stream: Optional[sd.OutputStream] = None
        self._mon_stream: Optional[sd.OutputStream] = None

        self._mic_sr = TARGET_SAMPLE_RATE
        self._mic_channels = 1
        self._mon_sr = TARGET_SAMPLE_RATE
        self._mon_channels = 2

        self.active_mic_device: Optional[AudioDeviceInfo] = None
        self.active_vout_device: Optional[AudioDeviceInfo] = None
        self.active_mon_device: Optional[AudioDeviceInfo] = None
        self.recommended_discord_mic: str = "Microphone (Steam Streaming Microphone)"

        # Live metering (0.0 to 1.0)
        self.mic_level = 0.0
        self.sb_level = 0.0
        self.vout_level = 0.0
        self._voice_duck_gain = 1.0
        self.last_error: Optional[str] = None
        self.running = False

    def list_devices(self) -> List[AudioDeviceInfo]:
        hostapis = sd.query_hostapis()
        wasapi_idx = next((i for i, h in enumerate(hostapis) if "WASAPI" in h["name"]), None)
        devices = sd.query_devices()
        result: List[AudioDeviceInfo] = []
        for idx, d in enumerate(devices):
            if wasapi_idx is not None and d["hostapi"] != wasapi_idx:
                continue
            name = str(d["name"])
            result.append(
                AudioDeviceInfo(
                    index=idx,
                    name=name,
                    hostapi=hostapis[d["hostapi"]]["name"],
                    max_inputs=int(d["max_input_channels"]),
                    max_outputs=int(d["max_output_channels"]),
                    default_samplerate=int(d["default_samplerate"]),
                    is_virtual=_is_virtual_name(name),
                )
            )
        # Fallback if WASAPI yielded nothing (e.g. headless test env)
        if not result:
            for idx, d in enumerate(devices):
                name = str(d["name"])
                result.append(
                    AudioDeviceInfo(
                        index=idx,
                        name=name,
                        hostapi=hostapis[d["hostapi"]]["name"],
                        max_inputs=int(d["max_input_channels"]),
                        max_outputs=int(d["max_output_channels"]),
                        default_samplerate=int(d["default_samplerate"]),
                        is_virtual=_is_virtual_name(name),
                    )
                )
        return result

    def _resolve_devices(self) -> Tuple[Optional[AudioDeviceInfo], Optional[AudioDeviceInfo], Optional[AudioDeviceInfo]]:
        devices = self.list_devices()
        cfg = self.store.config

        # 1. Virtual Output
        vout = None
        if cfg.virtual_out_device != "auto":
            vout = next((d for d in devices if d.max_outputs > 0 and d.name == cfg.virtual_out_device), None)
        if vout is None:
            for kw in VIRTUAL_OUT_KEYWORDS:
                vout = next((d for d in devices if d.max_outputs > 0 and kw in d.name.lower()), None)
                if vout:
                    break

        if vout:
            low = vout.name.lower()
            if "steam streaming microphone" in low:
                self.recommended_discord_mic = "Microphone (Steam Streaming Microphone)"
            elif "cable input" in low:
                self.recommended_discord_mic = "CABLE Output (VB-Audio Virtual Cable)"
            else:
                self.recommended_discord_mic = vout.name

        # 2. Physical Mic
        mic = None
        if cfg.mic_device != "auto":
            mic = next((d for d in devices if d.max_inputs > 0 and d.name == cfg.mic_device), None)
        if mic is None:
            try:
                default_in = sd.default.device[0]
                if default_in is not None and default_in >= 0:
                    cand = next((d for d in devices if d.index == default_in and not d.is_virtual), None)
                    if cand:
                        mic = cand
            except Exception:
                pass
        if mic is None:
            mic = next((d for d in devices if d.max_inputs > 0 and not d.is_virtual), None)

        # 3. Local Monitor Output
        mon = None
        if cfg.monitor_out_device != "auto":
            mon = next((d for d in devices if d.max_outputs > 0 and d.name == cfg.monitor_out_device), None)
        if mon is None:
            try:
                default_out = sd.default.device[1]
                if default_out is not None and default_out >= 0:
                    cand = next((d for d in devices if d.index == default_out and not d.is_virtual), None)
                    if cand:
                        mon = cand
            except Exception:
                pass
        if mon is None:
            mon = next((d for d in devices if d.max_outputs > 0 and not d.is_virtual), None)

        return mic, vout, mon

    def get_effective_vout_gain_db(self) -> float:
        cfg = self.store.config
        if cfg.virtual_out_gain_db is not None:
            return float(cfg.virtual_out_gain_db)
        if self.active_vout_device and "steam streaming microphone" in self.active_vout_device.name.lower():
            return -30.0
        return 0.0

    def _start_mic_stream(self, mic: AudioDeviceInfo) -> None:
        """Start physical mic capture; fall back to DirectSound/MME if WASAPI fails."""
        candidates = [mic]
        # Add non-WASAPI fallback with the same device name in case WASAPI throws WdmSyncIoctl
        for idx, d in enumerate(sd.query_devices()):
            if int(d["max_input_channels"]) > 0 and str(d["name"]) == mic.name and idx != mic.index:
                candidates.append(
                    AudioDeviceInfo(
                        index=idx,
                        name=str(d["name"]),
                        hostapi="fallback",
                        max_inputs=int(d["max_input_channels"]),
                        max_outputs=0,
                        default_samplerate=int(d["default_samplerate"]),
                        is_virtual=mic.is_virtual,
                    )
                )
        last_exc: Optional[Exception] = None
        for cand in candidates:
            try:
                sr = cand.default_samplerate or TARGET_SAMPLE_RATE
                ch = min(2, max(1, cand.max_inputs))
                stream = sd.InputStream(
                    device=cand.index,
                    samplerate=sr,
                    channels=ch,
                    dtype="float32",
                    blocksize=int(BLOCKSIZE * sr / TARGET_SAMPLE_RATE),
                    callback=self._on_mic_input,
                )
                stream.start()
                self._mic_sr = sr
                self._mic_channels = ch
                self._mic_stream = stream
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("Mic stream candidate %s (%s) failed: %s", cand.index, cand.hostapi, exc)
        if last_exc:
            raise last_exc

    def start(self) -> None:
        with self._lock:
            self.stop()
            self.last_error = None
            mic, vout, mon = self._resolve_devices()
            self.active_mic_device = mic
            self.active_vout_device = vout
            self.active_mon_device = mon

            errors: List[str] = []

            if mic is not None:
                try:
                    self._start_mic_stream(mic)
                except Exception as exc:
                    errors.append(f"Mic: {exc}")
                    logger.exception("Failed to start mic stream (continuing without mic passthrough)")

            if vout is not None:
                try:
                    vout_ch = min(2, max(1, vout.max_outputs))
                    self._vout_stream = sd.OutputStream(
                        device=vout.index,
                        samplerate=TARGET_SAMPLE_RATE,
                        channels=vout_ch,
                        dtype="float32",
                        blocksize=BLOCKSIZE,
                        callback=self._on_virtual_output,
                    )
                    self._vout_stream.start()
                except Exception as exc:
                    errors.append(f"VirtualOut: {exc}")
                    logger.exception("Failed to start virtual output stream")

            if mon is not None:
                try:
                    self._mon_sr = mon.default_samplerate or TARGET_SAMPLE_RATE
                    self._mon_channels = min(2, max(1, mon.max_outputs))
                    self._mon_stream = sd.OutputStream(
                        device=mon.index,
                        samplerate=self._mon_sr,
                        channels=self._mon_channels,
                        dtype="float32",
                        blocksize=int(BLOCKSIZE * self._mon_sr / TARGET_SAMPLE_RATE),
                        callback=self._on_monitor_output,
                    )
                    self._mon_stream.start()
                except Exception as exc:
                    errors.append(f"Monitor: {exc}")
                    logger.exception("Failed to start monitor stream")

            self.last_error = "; ".join(errors) if errors else None
            self.running = self._vout_stream is not None
            logger.info(
                "AudioEngine started | running=%s | mic=%s | vout=%s | mon=%s | vout_gain_db=%.1f",
                self.running,
                mic.name if mic else None,
                vout.name if vout else None,
                mon.name if mon else None,
                self.get_effective_vout_gain_db(),
            )

    def stop(self) -> None:
        with self._lock:
            for s in (self._mic_stream, self._vout_stream, self._mon_stream):
                if s is not None:
                    try:
                        s.stop()
                        s.close()
                    except Exception:
                        pass
            self._mic_stream = None
            self._vout_stream = None
            self._mon_stream = None
            self.mic_ring.clear()
            self.monitor_ring.clear()
            self.running = False

    def _on_mic_input(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        arr = indata
        if arr.ndim == 1:
            arr = np.column_stack([arr, arr])
        elif arr.shape[1] == 1:
            arr = np.column_stack([arr[:, 0], arr[:, 0]])
        elif arr.shape[1] > 2:
            arr = arr[:, :2]

        if self._mic_sr != TARGET_SAMPLE_RATE:
            arr = resample_linear(arr, self._mic_sr, TARGET_SAMPLE_RATE)

        rms = float(np.sqrt(np.mean(arr * arr))) if len(arr) else 0.0
        self.mic_level = 0.7 * self.mic_level + 0.3 * min(1.0, rms * 3.5)

        cfg = self.store.config
        if cfg.mic_passthrough and not cfg.mic_muted:
            self.mic_ring.write(arr * float(cfg.mic_gain))

    def _on_virtual_output(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        cfg = self.store.config
        mic_chunk = self.mic_ring.read(frames) if (cfg.mic_passthrough and not cfg.mic_muted) else np.zeros((frames, 2), dtype=np.float32)

        sb_chunk = np.zeros((frames, 2), dtype=np.float32)
        has_active = False
        finished_ids: List[str] = []
        with self._lock:
            remaining: List[ActivePlayback] = []
            for pb in self.active_playbacks:
                has_active = True
                need = frames
                write_pos = 0
                while need > 0:
                    avail = len(pb.pcm) - pb.pos
                    take = min(need, avail)
                    if take > 0:
                        sb_chunk[write_pos : write_pos + take] += pb.pcm[pb.pos : pb.pos + take] * pb.volume
                        pb.pos += take
                        write_pos += take
                        need -= take
                    if pb.pos >= len(pb.pcm):
                        if pb.loop and len(pb.pcm) > 0:
                            pb.pos = 0
                        else:
                            break
                if pb.pos < len(pb.pcm) or pb.loop:
                    remaining.append(pb)
                else:
                    finished_ids.append(pb.pad_id)
            self.active_playbacks = remaining

        if cfg.duck_music_on_voice and cfg.mic_passthrough and not cfg.mic_muted and self.mic_level > 0.08:
            target_duck = 0.25
            self._voice_duck_gain += (target_duck - self._voice_duck_gain) * 0.18
        else:
            self._voice_duck_gain += (1.0 - self._voice_duck_gain) * 0.025

        sb_chunk *= float(cfg.soundboard_gain) * self._voice_duck_gain
        sb_rms = float(np.sqrt(np.mean(sb_chunk * sb_chunk))) if len(sb_chunk) else 0.0
        self.sb_level = 0.65 * self.sb_level + 0.35 * min(1.0, sb_rms * 3.0)

        if cfg.monitor_enabled:
            self.monitor_ring.write(sb_chunk)

        if has_active and cfg.duck_mic:
            mic_chunk *= float(cfg.duck_amount)

        if finished_ids and cfg.queue_mode and not self.active_playbacks:
            last_id = finished_ids[-1]
            threading.Thread(target=self._advance_queue, args=(last_id,), daemon=True).start()

        mixed = np.tanh(mic_chunk + sb_chunk)
        vout_rms = float(np.sqrt(np.mean(mixed * mixed))) if len(mixed) else 0.0
        self.vout_level = 0.65 * self.vout_level + 0.35 * min(1.0, vout_rms * 3.0)

        gain_db = self.get_effective_vout_gain_db()
        gain_lin = 10.0 ** (gain_db / 20.0)
        scaled = mixed * gain_lin

        if outdata.shape[1] == 1:
            outdata[:, 0] = np.mean(scaled, axis=1)
        else:
            outdata[:, :2] = scaled

    def _on_monitor_output(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        cfg = self.store.config
        if not cfg.monitor_enabled:
            self.monitor_ring.clear()
            outdata.fill(0.0)
            return

        src_frames = max(1, int(round(frames * TARGET_SAMPLE_RATE / float(self._mon_sr))))
        chunk = self.monitor_ring.read(src_frames)
        if self._mon_sr != TARGET_SAMPLE_RATE:
            chunk = resample_linear(chunk, TARGET_SAMPLE_RATE, self._mon_sr)

        if len(chunk) < frames:
            pad = np.zeros((frames - len(chunk), 2), dtype=np.float32)
            chunk = np.vstack([chunk, pad])
        elif len(chunk) > frames:
            chunk = chunk[:frames]

        scaled = np.tanh(chunk * float(cfg.monitor_gain))
        outdata.fill(0.0)
        ch = min(2, outdata.shape[1])
        outdata[:, :ch] = scaled[:, :ch]

    def sync_sounds_directory(self) -> None:
        """Scan sounds/ folder and register any new audio files."""
        with self._lock:
            existing_files = {p.filename: pid for pid, p in self.store.config.pads.items()}
            for path in sorted(self.sounds_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if path.name not in existing_files:
                    pid = self._make_pad_id(path.stem)
                    self.store.config.pads[pid] = PadConfig(
                        id=pid,
                        name=path.stem.replace("_", " ").replace("-", " ").title(),
                        filename=path.name,
                    )
            # Prune missing files
            to_del = [
                pid for pid, pad in self.store.config.pads.items()
                if not (self.sounds_dir / pad.filename).exists()
            ]
            for pid in to_del:
                del self.store.config.pads[pid]
                self.clips.pop(pid, None)
            self.store.save()

    def _make_pad_id(self, stem: str) -> str:
        base = "".join(c.lower() if c.isalnum() else "_" for c in stem).strip("_") or "clip"
        pid = base
        idx = 2
        while pid in self.store.config.pads:
            pid = f"{base}_{idx}"
            idx += 1
        return pid

    def load_clip(self, pad_id: str) -> LoadedClip:
        with self._lock:
            if pad_id in self.clips:
                return self.clips[pad_id]
            pad = self.store.config.pads.get(pad_id)
            if not pad:
                raise KeyError(f"Unknown pad_id: {pad_id}")
            pcm, sr = decode_audio_file(self.sounds_dir / pad.filename, TARGET_SAMPLE_RATE)
            dur = len(pcm) / float(sr) if sr else 0.0
            clip = LoadedClip(pad_id=pad_id, pcm=pcm, duration_sec=dur)
            self.clips[pad_id] = clip
            return clip

    def _advance_queue(self, finished_pad_id: str) -> None:
        pad_ids = list(self.store.config.pads.keys())
        if len(pad_ids) < 2 or finished_pad_id not in pad_ids:
            return
        next_idx = (pad_ids.index(finished_pad_id) + 1) % len(pad_ids)
        next_id = pad_ids[next_idx]
        try:
            self.play_pad(next_id, stack=True)
        except Exception as exc:
            logger.warning("Queue advance failed for %s: %s", next_id, exc)

    def play_pad(self, pad_id: str, stack: bool = False) -> dict:
        pad = self.store.config.pads.get(pad_id)
        if not pad:
            raise KeyError(f"Pad not found: {pad_id}")

        if not stack:
            with self._lock:
                if any(pb.pad_id == pad_id for pb in self.active_playbacks):
                    self.active_playbacks = [pb for pb in self.active_playbacks if pb.pad_id != pad_id]
                    return {"stopped": pad_id}

        clip = self.load_clip(pad_id)
        pcm = clip.pcm
        start_idx = max(0, int(round(float(pad.start_sec or 0.0) * TARGET_SAMPLE_RATE)))
        end_idx = len(pcm)
        if pad.end_sec is not None and float(pad.end_sec) > float(pad.start_sec or 0.0):
            end_idx = min(len(pcm), int(round(float(pad.end_sec) * TARGET_SAMPLE_RATE)))
        if start_idx < end_idx:
            pcm = pcm[start_idx:end_idx]

        with self._lock:
            if not self.store.config.polyphonic:
                self.active_playbacks.clear()
            self.active_playbacks.append(
                ActivePlayback(
                    pad_id=pad_id,
                    pcm=pcm,
                    pos=0,
                    volume=float(pad.volume),
                    loop=bool(pad.loop),
                )
            )
        dur = len(pcm) / float(TARGET_SAMPLE_RATE)
        return {"playing": pad_id, "duration_sec": round(dur, 2)}

    def stop_pad(self, pad_id: str) -> None:
        with self._lock:
            self.active_playbacks = [pb for pb in self.active_playbacks if pb.pad_id != pad_id]

    def stop_all(self) -> None:
        with self._lock:
            self.active_playbacks.clear()
            self.monitor_ring.clear()

    def get_playing_status(self) -> Dict[str, float]:
        with self._lock:
            res: Dict[str, float] = {}
            for pb in self.active_playbacks:
                if len(pb.pcm) > 0:
                    res[pb.pad_id] = round(min(1.0, pb.pos / float(len(pb.pcm))), 3)
            return res

    def calibrate_steam_loopback(self) -> dict:
        """Measure actual loopback gain from Virtual Output -> Virtual Input and auto-adjust dB."""
        devices = self.list_devices()
        vout = self.active_vout_device
        if vout is None:
            raise RuntimeError("No virtual output device active")

        vin = next((d for d in devices if d.max_inputs > 0 and "steam streaming microphone" in d.name.lower()), None)
        if vin is None:
            raise RuntimeError("Matching virtual microphone input not found for loopback calibration")

        sr = TARGET_SAMPLE_RATE
        dur = 0.25
        t = np.linspace(0, dur, int(sr * dur), endpoint=False, dtype=np.float32)
        probe_amp = 0.02
        tone = probe_amp * np.sin(2 * np.pi * 1000 * t)
        stereo = np.column_stack([tone, tone])

        rec = np.zeros_like(stereo)
        idx = 0

        def cb_in(indata, frames, time_info, status):
            nonlocal idx
            take = min(frames, len(rec) - idx)
            if take > 0:
                ch = min(2, indata.shape[1])
                rec[idx : idx + take, :ch] = indata[:take, :ch]
                idx += take

        was_running = self.running
        if was_running:
            self.stop()
        try:
            with sd.InputStream(device=vin.index, samplerate=sr, channels=min(2, vin.max_inputs), dtype="float32", callback=cb_in):
                sd.play(stereo, samplerate=sr, device=vout.index, blocking=True)
                time.sleep(0.08)
        finally:
            if was_running:
                self.start()

        measured_peak = float(np.max(np.abs(rec))) if len(rec) else 0.0
        if measured_peak < 1e-4:
            return {"ok": False, "reason": "No loopback signal detected", "measured_peak": measured_peak}

        driver_gain = measured_peak / probe_amp
        recommended_db = round(-20.0 * math.log10(driver_gain), 1)
        self.store.update(virtual_out_gain_db=recommended_db)
        return {
            "ok": True,
            "driver_gain_linear": round(driver_gain, 2),
            "recommended_db": recommended_db,
        }
