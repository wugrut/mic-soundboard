"""FastAPI server & WebSocket telemetry for the Mic Soundboard UI."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio_decoder import SUPPORTED_EXTENSIONS
from .audio_engine import AudioEngine
from .config_store import ConfigStore, PadConfig
from .hotkeys import GlobalHotkeyManager
from .win_default_mic import DefaultMicSwitcher
from .youtube_importer import import_youtube_audio

logger = logging.getLogger("mic_soundboard.server")


class YouTubeImportRequest(BaseModel):
    url: str
    auto_play: bool = False


class SettingsUpdate(BaseModel):
    mic_device: Optional[str] = None
    virtual_out_device: Optional[str] = None
    monitor_out_device: Optional[str] = None
    mic_passthrough: Optional[bool] = None
    mic_muted: Optional[bool] = None
    mic_gain: Optional[float] = None
    soundboard_gain: Optional[float] = None
    monitor_enabled: Optional[bool] = None
    monitor_gain: Optional[float] = None
    virtual_out_gain_db: Optional[float] = None
    polyphonic: Optional[bool] = None
    duck_mic: Optional[bool] = None
    duck_amount: Optional[float] = None
    duck_music_on_voice: Optional[bool] = None
    queue_mode: Optional[bool] = None
    auto_switch_default_mic: Optional[bool] = None
    stop_all_hotkey: Optional[str] = None


class PadUpdate(BaseModel):
    name: Optional[str] = None
    volume: Optional[float] = None
    hotkey: Optional[str] = None
    color: Optional[str] = None
    loop: Optional[bool] = None
    start_sec: Optional[float] = None
    end_sec: Optional[float] = None


def create_app(base_dir: Path, start_audio: bool = True) -> FastAPI:
    app = FastAPI(title="Mic Soundboard & Audio Injector")
    sounds_dir = base_dir / "sounds"
    config_path = base_dir / "config.json"
    static_dir = Path(__file__).parent / "static"

    store = ConfigStore(config_path)
    engine = AudioEngine(store, sounds_dir)
    hotkeys = GlobalHotkeyManager(store, engine)
    mic_switcher = DefaultMicSwitcher(base_dir / ".default_mic_backup.json")

    app.state.store = store
    app.state.engine = engine
    app.state.hotkeys = hotkeys
    app.state.mic_switcher = mic_switcher

    @app.on_event("startup")
    def _startup() -> None:
        engine.sync_sounds_directory()
        if start_audio:
            engine.start()
            hotkeys.start()
            if store.config.auto_switch_default_mic and engine.recommended_discord_mic:
                mic_switcher.activate(engine.recommended_discord_mic)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        hotkeys.stop()
        engine.stop()
        if start_audio:
            mic_switcher.restore()

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/state")
    def get_state():
        engine.sync_sounds_directory()
        devices = [asdict(d) for d in engine.list_devices()]
        cfg = store.config
        pads = [asdict(p) for p in cfg.pads.values()]
        return {
            "running": engine.running,
            "last_error": engine.last_error,
            "recommended_discord_mic": engine.recommended_discord_mic,
            "auto_mic_switched": mic_switcher.active,
            "effective_vout_gain_db": engine.get_effective_vout_gain_db(),
            "active_devices": {
                "mic": engine.active_mic_device.name if engine.active_mic_device else None,
                "virtual_out": engine.active_vout_device.name if engine.active_vout_device else None,
                "monitor_out": engine.active_mon_device.name if engine.active_mon_device else None,
            },
            "devices": devices,
            "settings": {
                "mic_device": cfg.mic_device,
                "virtual_out_device": cfg.virtual_out_device,
                "monitor_out_device": cfg.monitor_out_device,
                "mic_passthrough": cfg.mic_passthrough,
                "mic_muted": cfg.mic_muted,
                "mic_gain": cfg.mic_gain,
                "soundboard_gain": cfg.soundboard_gain,
                "monitor_enabled": cfg.monitor_enabled,
                "monitor_gain": cfg.monitor_gain,
                "virtual_out_gain_db": cfg.virtual_out_gain_db,
                "polyphonic": cfg.polyphonic,
                "duck_mic": cfg.duck_mic,
                "duck_amount": cfg.duck_amount,
                "duck_music_on_voice": cfg.duck_music_on_voice,
                "queue_mode": cfg.queue_mode,
                "auto_switch_default_mic": cfg.auto_switch_default_mic,
                "stop_all_hotkey": cfg.stop_all_hotkey,
            },
            "pads": pads,
            "playing": engine.get_playing_status(),
        }

    @app.post("/api/settings")
    def update_settings(body: SettingsUpdate):
        updates = body.model_dump(exclude_none=True)
        restart_needed = any(
            k in updates for k in ("mic_device", "virtual_out_device", "monitor_out_device")
        )
        store.update(**updates)
        if restart_needed and start_audio:
            engine.start()
        if "auto_switch_default_mic" in updates and start_audio:
            if store.config.auto_switch_default_mic and engine.recommended_discord_mic:
                mic_switcher.activate(engine.recommended_discord_mic)
            else:
                mic_switcher.restore()
        return {"ok": True, "effective_vout_gain_db": engine.get_effective_vout_gain_db()}

    @app.post("/api/pads/upload")
    async def upload_pad(file: UploadFile = File(...)):
        if not file.filename:
            raise HTTPException(status_code=400, detail="Missing filename")
        ext = Path(file.filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported audio format '{ext}'. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
            )
        safe_name = Path(file.filename).name
        dest = sounds_dir / safe_name
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        engine.sync_sounds_directory()
        pad = next((p for p in store.config.pads.values() if p.filename == safe_name), None)
        if pad:
            try:
                engine.load_clip(pad.id)
            except Exception as exc:
                dest.unlink(missing_ok=True)
                engine.sync_sounds_directory()
                raise HTTPException(status_code=400, detail=f"Failed to decode audio: {exc}")
        return {"ok": True, "pad": asdict(pad) if pad else None}

    @app.post("/api/pads/youtube")
    def import_youtube(body: YouTubeImportRequest):
        try:
            tracks = import_youtube_audio(body.url, sounds_dir)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        engine.sync_sounds_directory()
        added_pads = []
        for downloaded, title in tracks:
            pad = next((p for p in store.config.pads.values() if p.filename == downloaded.name), None)
            if pad:
                pad.name = title[:60]
                store.save()
                try:
                    engine.load_clip(pad.id)
                    added_pads.append(asdict(pad))
                except Exception as exc:
                    downloaded.unlink(missing_ok=True)
                    engine.sync_sounds_directory()
                    logger.warning("Failed to decode %s: %s", downloaded.name, exc)

        if not added_pads:
            raise HTTPException(status_code=400, detail="Could not decode any audio tracks from that URL")

        if body.auto_play:
            engine.play_pad(added_pads[0]["id"])

        return {"ok": True, "pad": added_pads[0], "pads": added_pads}

    @app.post("/api/pads/{pad_id}/play")
    def play_pad(pad_id: str, stack: bool = False):
        try:
            return engine.play_pad(pad_id, stack=stack)
        except KeyError:
            raise HTTPException(status_code=404, detail="Pad not found")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/pads/{pad_id}/stop")
    def stop_pad(pad_id: str):
        engine.stop_pad(pad_id)
        return {"ok": True}

    @app.post("/api/stop_all")
    def stop_all():
        engine.stop_all()
        return {"ok": True}

    @app.patch("/api/pads/{pad_id}")
    def update_pad(pad_id: str, body: PadUpdate):
        pad = store.config.pads.get(pad_id)
        if not pad:
            raise HTTPException(status_code=404, detail="Pad not found")
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(pad, k, v)
        store.save()
        return {"ok": True, "pad": asdict(pad)}

    @app.delete("/api/pads/{pad_id}")
    def delete_pad(pad_id: str):
        pad = store.config.pads.get(pad_id)
        if not pad:
            raise HTTPException(status_code=404, detail="Pad not found")
        engine.stop_pad(pad_id)
        file_path = sounds_dir / pad.filename
        file_path.unlink(missing_ok=True)
        del store.config.pads[pad_id]
        engine.clips.pop(pad_id, None)
        store.save()
        return {"ok": True}

    @app.post("/api/calibrate")
    def calibrate_virtual_mic():
        try:
            return engine.calibrate_steam_loopback()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.websocket("/ws/meters")
    async def ws_meters(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(
                    {
                        "mic": round(engine.mic_level, 3),
                        "sb": round(engine.sb_level, 3),
                        "vout": round(engine.vout_level, 3),
                        "playing": engine.get_playing_status(),
                    }
                )
                await asyncio.sleep(0.05)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    return app
