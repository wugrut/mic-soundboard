"""Persistent JSON configuration for the soundboard & audio router."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class PadConfig:
    id: str
    name: str
    filename: str
    volume: float = 1.0
    hotkey: str = ""
    color: str = "#5865f2"
    loop: bool = False
    start_sec: float = 0.0
    end_sec: Optional[float] = None


@dataclass
class AppConfig:
    mic_device: str = "auto"
    virtual_out_device: str = "auto"
    monitor_out_device: str = "auto"
    mic_passthrough: bool = True
    mic_muted: bool = False
    mic_gain: float = 1.0
    soundboard_gain: float = 0.85
    monitor_enabled: bool = True
    monitor_gain: float = 0.65
    virtual_out_gain_db: Optional[float] = None  # None = auto (-30dB for Steam, 0dB for others)
    polyphonic: bool = True
    duck_mic: bool = False
    duck_amount: float = 0.35
    duck_music_on_voice: bool = False
    queue_mode: bool = False
    auto_switch_default_mic: bool = True
    stop_all_hotkey: str = "f12"
    pads: Dict[str, PadConfig] = field(default_factory=dict)


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.config = self._load()

    def _load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            pads_raw = raw.pop("pads", {}) or {}
            pads = {
                pid: PadConfig(**pdata)
                for pid, pdata in pads_raw.items()
                if isinstance(pdata, dict)
            }
            valid_fields = {f for f in AppConfig.__dataclass_fields__ if f != "pads"}
            filtered = {k: v for k, v in raw.items() if k in valid_fields}
            return AppConfig(**filtered, pads=pads)
        except Exception:
            return AppConfig()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = asdict(self.config)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def update(self, **kwargs) -> AppConfig:
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self.config, k) and k != "pads":
                    setattr(self.config, k, v)
            self.save()
            return self.config
