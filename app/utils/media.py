"""Content sniffing and header parsing for images and audio (no Pillow / ffmpeg needed)."""

from __future__ import annotations

import struct

IMAGE_TYPES = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "image/gif": b"GIF8",
}


def sniff_image(head: bytes) -> str | None:
    for mime, magic in IMAGE_TYPES.items():
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_audio(head: bytes) -> str | None:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"ID3") or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "audio/x-m4a" if brand in (b"M4A ", b"M4B ") else "audio/mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the container header; None when it cannot be read."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return int(w), int(h)
        if data.startswith(b"GIF8") and len(data) >= 10:
            w, h = struct.unpack("<HH", data[6:10])
            return int(w), int(h)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return int(w & 0x3FFF), int(h & 0x3FFF)
            if chunk == b"VP8L":
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return int(w), int(h)
            if chunk == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return int(w), int(h)
        if data.startswith(b"\xff\xd8\xff"):
            i = 2
            n = len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = struct.unpack(">H", data[i + 2 : i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                    return int(w), int(h)
                i += 2 + length
    except (struct.error, IndexError, ValueError):
        return None
    return None


def wav_bytes(samples: bytes, *, sample_rate: int = 8000, channels: int = 1, bits: int = 16) -> bytes:
    """Wrap raw PCM samples in a RIFF/WAVE container."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
    header += b"data" + struct.pack("<I", len(samples))
    return header + samples


def wav_duration_seconds(data: bytes) -> float | None:
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE" or len(data) < 44:
        return None
    try:
        channels, sample_rate = struct.unpack("<HI", data[22:28])
        bits = struct.unpack("<H", data[34:36])[0]
        data_len = struct.unpack("<I", data[40:44])[0]
        if not channels or not sample_rate or not bits:
            return None
        return data_len / (sample_rate * channels * bits / 8)
    except struct.error:
        return None
