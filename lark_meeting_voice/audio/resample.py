"""High-quality PCM resampling using scipy.signal.resample_poly.

All audio is mono s16le. 24 kHz <-> 16 kHz uses ratio 2:3.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import resample_poly


def _bytes_to_int16(pcm: bytes) -> np.ndarray:
    if len(pcm) % 2 != 0:
        pcm = pcm[: len(pcm) - 1]
    return np.frombuffer(pcm, dtype=np.int16)


def _int16_to_bytes(arr: np.ndarray) -> bytes:
    return arr.astype(np.int16).tobytes()


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    arr = _bytes_to_int16(pcm).astype(np.float32)
    # Reduce ratio to avoid huge filter kernels.
    from math import gcd
    g = gcd(src_rate, dst_rate)
    up = dst_rate // g
    down = src_rate // g
    out = resample_poly(arr, up, down)
    out = np.clip(out, -32768, 32767).astype(np.int16)
    return _int16_to_bytes(out)


def downsample_24k_to_16k(pcm: bytes) -> bytes:
    return resample_pcm(pcm, 24000, 16000)


def upsample_16k_to_24k(pcm: bytes) -> bytes:
    return resample_pcm(pcm, 16000, 24000)
