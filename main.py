"""Entry point for Mic Soundboard & Steam Virtual Mic Injector."""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from app.server import create_app


def _setup_logging(base_dir: Path) -> None:
    log_path = base_dir / "soundboard.log"
    # Under pythonw.exe on Windows, sys.stdout and sys.stderr are None,
    # which causes uvicorn's default formatter or print() to crash.
    if sys.stdout is None:
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _open_app_window(url: str, delay: float = 0.9) -> None:
    if delay > 0:
        time.sleep(delay)
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        shutil.which("msedge"),
        shutil.which("chrome"),
    ]
    for exe in candidates:
        if exe and Path(exe).exists():
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1040,740"])
                return
            except Exception:
                pass
    webbrowser.open(url)


def main() -> None:
    base_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    _setup_logging(base_dir)
    logger = logging.getLogger("mic_soundboard.main")

    port = 8765
    url = f"http://127.0.0.1:{port}"

    # If already running, just re-open the UI window and exit cleanly.
    if _is_port_in_use(port):
        logger.info("Soundboard already running at %s; opening window.", url)
        _open_app_window(url, delay=0.0)
        return

    try:
        app = create_app(base_dir=base_dir, start_audio=True)
        threading.Thread(target=_open_app_window, args=(url,), daemon=True).start()
        logger.info("Starting Mic Soundboard server at %s", url)
        uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)
    except Exception:
        logger.exception("Fatal error starting Mic Soundboard")
        raise


if __name__ == "__main__":
    main()
