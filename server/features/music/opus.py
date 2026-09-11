"""Opus streaming encodes for generated music — stdlib + numpy only.

Renders are CD-quality WAV (~1.4 Mbps); serving them whole to the chat
player wastes ~10 MB per minute. This module transcodes to Ogg Opus
(~64 kbps, ~22x smaller) via ctypes against the system's libopus (already
present as a dependency — no ffmpeg, no pip packages), plus a numpy
windowed-sinc resampler (Opus has no 44.1 kHz mode) and a hand-rolled Ogg
muxer.

Anything that can fail (missing lib, encode error) raises; callers treat a
missing .opus sidecar as "play the WAV".
"""

import ctypes
import os
import struct
import wave
import zlib

TARGET_SR = 48000
FRAME_SAMPLES = 960          # 20 ms @ 48 kHz
MAX_PACKET = 4000
OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_GET_LOOKAHEAD_REQUEST = 4027

_lib = None


def _load_lib():
    """ctypes handle for libopus, or raise."""
    global _lib
    if _lib is not None:
        return _lib
    import ctypes.util
    name = ctypes.util.find_library("opus")
    if not name:
        raise RuntimeError("libopus not found")
    lib = ctypes.CDLL(name)
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int32, ctypes.c_int, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int)]
    lib.opus_encoder_create.restype = ctypes.c_void_p
    # opus_encoder_ctl is variadic: declare the two FIXED params so the
    # encoder pointer passes through whole. Extra varargs then use default
    # conversions (int -> c_int, byref stays a real pointer). Leaving
    # argtypes unset entirely would truncate the 64-bit pointer to c_int
    # and segfault inside libopus.
    lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.opus_encoder_ctl.restype = ctypes.c_int32
    lib.opus_encode.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_encoder_destroy.restype = None
    lib.opus_strerror = None
    try:
        lib.opus_strerror.argtypes = [ctypes.c_int]
        lib.opus_strerror.restype = ctypes.c_char_p
    except AttributeError:
        pass
    _lib = lib
    return lib


def libopus_available():
    try:
        _load_lib()
        return True
    except Exception:
        return False


def _resample_to_48k(samples, sr):
    """Float64 (n, ch) at sr -> (m, ch) at 48 kHz. FFT resample is exact
    for offline use and far cleaner than a hand-rolled polyphase filter."""
    import numpy as np
    if sr == TARGET_SR:
        return np.ascontiguousarray(samples, dtype=np.float64)
    n_in = samples.shape[0]
    n_out = int(round(n_in * TARGET_SR / sr))
    if n_out < 1:
        raise ValueError("empty audio")
    out = np.zeros((n_out, samples.shape[1]), dtype=np.float64)
    for ch in range(samples.shape[1]):
        spec = np.fft.rfft(samples[:, ch], n=n_in)
        need = n_out // 2 + 1
        have = spec.shape[0]
        adj = np.zeros(need, dtype=spec.dtype)
        adj[:min(have, need)] = spec[:min(have, need)]
        if n_in > 0:
            adj *= n_out / n_in
        out[:, ch] = np.fft.irfft(adj, n=n_out)
    return out


def _ogg_page(packets, granule, serial, seq, bos=False, eos=False):
    """One Ogg page carrying the given complete packets."""
    header_type = (0x02 if bos else 0) | (0x04 if eos else 0)
    table = bytearray()
    body = bytearray()
    for p in packets:
        n = len(p)
        while n >= 255:
            table.append(255)
            n -= 255
        table.append(n)
        body += p
    if len(table) > 255:
        raise ValueError("too many packets for one page")
    head = struct.pack(
        "<4sBBQIIiB", b"OggS", 0, header_type,
        0xFFFFFFFFFFFFFFFF if granule is None else granule,
        serial, seq, 0, len(table)) + bytes(table)
    page = bytearray(head) + body
    crc = zlib.crc32(bytes(page)) & 0xFFFFFFFF
    struct.pack_into("<I", page, 22, crc)
    return bytes(page)


def _opus_head(channels, preskip, input_sr):
    return (b"OpusHead" + struct.pack("<BBHIhB", 1, channels, preskip,
                                       input_sr, 0, 0))


def _opus_tags(vendor="local-ai opus.py"):
    vb = vendor.encode("utf-8")
    return (b"OpusTags" + struct.pack("<I", len(vb)) + vb
            + struct.pack("<I", 1)
            + struct.pack("<I", len(b"ENCODER=local-ai")) + b"ENCODER=local-ai")


def encode_wav_to_opus(in_path, out_path=None, bitrate=None):
    """Transcode a 16-bit PCM WAV to Ogg Opus. Returns out_path.

    Raises on any failure (missing libopus, unsupported layout, encode
    error) — callers fall back to serving the WAV.
    """
    import numpy as np
    if bitrate is None:
        try:
            bitrate = int(os.environ.get("MUSIC_OPUS_BITRATE", "64000"))
        except ValueError:
            bitrate = 64000
    if out_path is None:
        base, ext = os.path.splitext(in_path)
        if ext.lower() != ".wav":
            raise ValueError("expected a .wav input")
        out_path = base + ".opus"
    with wave.open(in_path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        sr = w.getframerate()
        nframes = w.getnframes()
        if nch not in (1, 2) or sw != 2 or nframes <= 0:
            raise ValueError(f"unsupported WAV layout: {nch}ch/{sw * 8}bit")
        raw = w.readframes(nframes)
    pcm = np.frombuffer(raw, dtype="<i2").astype(np.float64).reshape(-1, nch)
    if nch == 1:
        pcm = np.repeat(pcm, 2, axis=1)
        nch = 2
    pcm48 = _resample_to_48k(pcm, sr)
    total = pcm48.shape[0]
    pcm16 = np.clip(np.round(pcm48), -32768, 32767).astype("<i2")

    lib = _load_lib()
    err = ctypes.c_int(0)
    enc = lib.opus_encoder_create(TARGET_SR, nch, OPUS_APPLICATION_AUDIO,
                                  ctypes.byref(err))
    if not enc or err.value != 0:
        raise RuntimeError(f"opus_encoder_create failed: {err.value}")
    try:
        if lib.opus_encoder_ctl(enc, OPUS_SET_BITRATE_REQUEST,
                                int(bitrate)) != 0:
            raise RuntimeError("opus bitrate rejected")
        lookahead = ctypes.c_int32(0)
        if lib.opus_encoder_ctl(enc, OPUS_GET_LOOKAHEAD_REQUEST,
                                ctypes.byref(lookahead)) == 0:
            preskip = int(lookahead.value)
        else:
            preskip = 312  # encoder default at 48 kHz stereo
        out_buf = (ctypes.c_ubyte * MAX_PACKET)()
        packets = []
        pos = 0
        while pos < total:
            n = min(FRAME_SAMPLES, total - pos)
            frame = np.zeros((FRAME_SAMPLES, nch), dtype="<i2")
            frame[:n] = pcm16[pos:pos + n]
            ptr = frame.ctypes.data_as(ctypes.POINTER(ctypes.c_int16))
            nb = lib.opus_encode(enc, ptr, FRAME_SAMPLES, out_buf, MAX_PACKET)
            if nb < 0:
                msg = ""
                if lib.opus_strerror is not None:
                    try:
                        msg = lib.opus_strerror(nb).decode()
                    except Exception:
                        pass
                raise RuntimeError(f"opus_encode failed: {nb} {msg}")
            packets.append(bytes(out_buf[:nb]))
            pos += n
    finally:
        lib.opus_encoder_destroy(enc)

    import random
    serial = random.getrandbits(32)
    pages = []
    pages.append(_ogg_page([_opus_head(nch, preskip, sr)], 0, serial, 0,
                           bos=True))
    pages.append(_ogg_page([_opus_tags()], 0, serial, 1))
    done = 0
    seq = 2
    CHUNK = 10
    for i in range(0, len(packets), CHUNK):
        chunk = packets[i:i + CHUNK]
        done += len(chunk) * FRAME_SAMPLES
        last = (i + CHUNK) >= len(packets)
        granule = preskip + min(done, total) if last else None
        pages.append(_ogg_page(chunk, granule, serial, seq, eos=last))
        seq += 1
    with open(out_path, "wb") as f:
        for pg in pages:
            f.write(pg)
    return out_path
