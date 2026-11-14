"""Global keyboard hotkey listener for triggering soundboard pads in-game."""

from __future__ import annotations

import logging
import threading
from typing import Optional, Set

from pynput import keyboard

from .audio_engine import AudioEngine
from .config_store import ConfigStore

logger = logging.getLogger("mic_soundboard.hotkeys")

MODIFIER_NAMES = {
    "ctrl",
    "ctrl_l",
    "ctrl_r",
    "alt",
    "alt_l",
    "alt_r",
    "shift",
    "shift_l",
    "shift_r",
    "cmd",
}


def _normalize_key(key) -> str:
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            # Windows Numpad vk codes: 96..105 -> num0..num9
            if 96 <= key.vk <= 105:
                return f"num{key.vk - 96}"
            return f"vk_{key.vk}"
        return ""
    if isinstance(key, keyboard.Key):
        name = key.name.lower()
        if name.startswith("ctrl"):
            return "ctrl"
        if name.startswith("alt"):
            return "alt"
        if name.startswith("shift"):
            return "shift"
        return name
    return ""


class GlobalHotkeyManager:
    def __init__(self, store: ConfigStore, engine: AudioEngine):
        self.store = store
        self.engine = engine
        self._listener: Optional[keyboard.Listener] = None
        self._pressed_mods: Set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._listener is not None:
            return
        try:
            self._listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
            )
            self._listener.daemon = True
            self._listener.start()
            logger.info("Global hotkey listener started")
        except Exception as exc:
            logger.warning("Global hotkey listener unavailable: %s", exc)
            self._listener = None

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def _on_press(self, key) -> None:
        k = _normalize_key(key)
        if not k:
            return
        with self._lock:
            if k in ("ctrl", "alt", "shift"):
                self._pressed_mods.add(k)
                return
            mods = sorted(self._pressed_mods)
            combo = "+".join([*mods, k]) if mods else k

        cfg = self.store.config
        if cfg.stop_all_hotkey and combo == cfg.stop_all_hotkey.lower():
            self.engine.stop_all()
            return

        for pad in list(cfg.pads.values()):
            if pad.hotkey and pad.hotkey.strip().lower() == combo:
                try:
                    self.engine.play_pad(pad.id)
                except Exception as exc:
                    logger.warning("Hotkey playback failed for %s: %s", pad.id, exc)
                break

    def _on_release(self, key) -> None:
        k = _normalize_key(key)
        if k in ("ctrl", "alt", "shift"):
            with self._lock:
                self._pressed_mods.discard(k)
