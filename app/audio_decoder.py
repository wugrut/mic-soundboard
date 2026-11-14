"""Universal audio file decoder converting any media format to 48 kHz stereo float32."""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import av
import numpy as np

TARGET_SAMPLE_RATE = 48000
TARGET_CHANNELS = 2
SUPPORTED_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".ogg",
    ".flac",
    ".m4a",
    ".aac",
    ".mp4",
    ".webm",
    ".opus",
    ".wma",
}


def decode_audio_file(path: str | Path, target_sr: int = TARGET_SAMPLE_RATE) -> Tuple[np.ndarray, int]:
    """Decode an audio or video file into a float32 numpy array of shape (N, 2).

    Values are normalized in [-1.0, 1.0].
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    resampler = av.audio.resampler.AudioResampler(
        format="flt",
        layout="stereo",
        rate=target_sr,
    )

    chunks: list[np.ndarray] = []
    with av.open(str(file_path)) as container:
        audio_streams = [s for s in container.streams if s.type == "audio"]
        if not audio_streams:
            raise ValueError(f"No audio stream found in {file_path.name}")

        stream = audio_streams[0]
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                arr = resampled.to_ndarray()
                # PyAV planar 'flt' or packed 'flt':
                # format='flt' is packed float32: shape (1, samples * channels) or (channels, samples)
                if arr.ndim == 2:
                    if arr.shape[0] == TARGET_CHANNELS and arr.shape[1] != TARGET_CHANNELS:
                        arr = arr.T
                    elif arr.shape[0] == 1:
                        arr = arr.reshape(-1, TARGET_CHANNELS)
                else:
                    arr = arr.reshape(-1, TARGET_CHANNELS)
                chunks.append(arr.astype(np.float32, copy=False))

        # Flush resampler
        for resampled in resampler.resample(None):
            arr = resampled.to_ndarray()
            if arr.ndim == 2:
                if arr.shape[0] == TARGET_CHANNELS and arr.shape[1] != TARGET_CHANNELS:
                    arr = arr.T
                elif arr.shape[0] == 1:
                    arr = arr.reshape(-1, TARGET_CHANNELS)
            else:
                arr = arr.reshape(-1, TARGET_CHANNELS)
            chunks.append(arr.astype(np.float32, copy=False))

    if not chunks:
        return np.zeros((0, TARGET_CHANNELS), dtype=np.float32), target_sr

    pcm = np.vstack(chunks)
    # Guard against NaNs or infs in malformed files
    pcm = np.nan_to_num(pcm, nan=0.0, posinf=1.0, neginf=-1.0)
    return pcm, target_sr


def resample_linear(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Fast linear resampler for matching local monitor sample rate (e.g. 48000 -> 44100)."""
    if src_sr == dst_sr or len(audio) == 0:
        return audio
    ratio = dst_sr / float(src_sr)
    out_len = max(1, int(round(len(audio) * ratio)))
    x_old = np.linspace(0.0, 1.0, len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, out_len, endpoint=False)
    if audio.ndim == 1:
        return np.interp(x_new, x_old, audio).astype(np.float32)
    channels = [np.interp(x_new, x_old, audio[:, ch]) for ch in range(audio.shape[1])]
    return np.column_stack(channels).astype(np.float32)
