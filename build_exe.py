"""Build standalone single-file Mic_Soundboard.exe via PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def main() -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "Mic_Soundboard",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        "--add-data",
        f"{ROOT / 'app' / 'static'};app/static",
        "--collect-all",
        "av",
        "--collect-all",
        "_sounddevice_data",
        "--collect-all",
        "_soundfile_data",
        "--hidden-import",
        "pynput.keyboard._win32",
        "--hidden-import",
        "pynput.mouse._win32",
        "--hidden-import",
        "pycaw.pycaw",
        "--hidden-import",
        "comtypes",
        str(ROOT / "main.py"),
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    exe_path = ROOT / "dist" / "Mic_Soundboard.exe"
    size_mb = exe_path.stat().st_size / (1024 * 1024)
    print(f"\n[OK] Built standalone executable: {exe_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
