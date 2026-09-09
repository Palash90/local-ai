"""Text-to-speech engine shared by chat-webui (/api/tts) and markdown_hosting.

All TTS functions (cleanup, language detection, chunked synthesis, cache,
internal synthesis endpoint) live here. ``server/api.py`` and
``markdown_hosting.py`` both import from this module — synthesis logic is
never duplicated.

Importers must set ``TTS_CACHE_DIR``, ``TTS_CACHE_SECONDARY_DIR``,
``TTS_CACHE_MAX_BYTES``, ``TTS_MAX_CHARS``, ``TTS_MAX_CHARS_PUBLIC``,
``TTS_CHUNK_CHARS`` before first call (done via ``config.py`` constants).
"""
import base64
import hashlib
import io as _io
import os
import re as _re
import threading
import wave as _wave

from server.config import (
    TTS_CACHE_DIR,
    TTS_CACHE_MAX_BYTES,
    TTS_CACHE_SECONDARY_DIR,
    TTS_CHUNK_CHARS,
    TTS_MAX_CHARS,
    TTS_MAX_CHARS_PUBLIC,
)

# ---------------------------------------------------------------------------
# Voice catalogue — offline Piper (local) + online edge-tts (neural).
# ---------------------------------------------------------------------------

PIPER_VOICES = {
    "es": os.path.expanduser("~/.piper_voices/es_MX-claude-high.onnx"),
    "en": os.path.expanduser("~/.piper_voices/en_US-lessac-high.onnx"),
}
EDGE_VOICES = {
    "bn": "bn-BD-NabanitaNeural",
    "hi": "hi-IN-SwaraNeural",
    "te": "te-IN-ShrutiNeural",
    "kn": "kn-IN-SapnaNeural",
    "es": "es-MX-DaliaNeural",
    "en": "en-US-AriaNeural",
}

# ---------------------------------------------------------------------------
# Shared lock + process-wide caches (memory + primary disk).
# ---------------------------------------------------------------------------

_PIPER_LOCK = threading.Lock()
_PIPER_VOICES = {}
_TTS_AUDIO_CACHE = {}
_TTS_AUDIO_CACHE_ORDER = []
_TTS_AUDIO_CACHE_MAX = 64


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _tts_ext_for(data):
    """Sniff audio container from magic bytes."""
    if data[:4] == b"RIFF":
        return ".wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ".mp3"
    return ""


def _tts_cache_get(key):
    with _PIPER_LOCK:
        hit = _TTS_AUDIO_CACHE.get(key)
    if hit is not None:
        return hit
    # Disk tiers: primary first, then the (optional) secondary archive.
    for ext in (".wav", ".mp3"):
        for base in (TTS_CACHE_DIR, TTS_CACHE_SECONDARY_DIR or None):
            if not base:
                continue
            try:
                path = os.path.join(base, key + ext)
                if not os.path.isfile(path):
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                if not data or _tts_ext_for(data) != ext:
                    if base == TTS_CACHE_DIR:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    continue
                print(f"[tts] disk cache hit ({base}, {len(data)} bytes)")
                with _PIPER_LOCK:
                    _TTS_AUDIO_CACHE[key] = data
                return data
            except OSError:
                continue
    return None


def _tts_prune_disk_cache():
    """Enforce TTS_CACHE_MAX_BYTES on the primary dir (oldest-mtime-first)."""
    try:
        files = []
        total = 0
        for name in os.listdir(TTS_CACHE_DIR):
            if not (name.endswith(".wav") or name.endswith(".mp3")):
                continue
            path = os.path.join(TTS_CACHE_DIR, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
            total += st.st_size
        files.sort()
        for _, size, path in files:
            if total <= TTS_CACHE_MAX_BYTES:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue
    except OSError:
        pass


def _tts_cache_put(key, value):
    with _PIPER_LOCK:
        _TTS_AUDIO_CACHE[key] = value
        _TTS_AUDIO_CACHE_ORDER.append(key)
        while len(_TTS_AUDIO_CACHE_ORDER) > _TTS_AUDIO_CACHE_MAX:
            old = _TTS_AUDIO_CACHE_ORDER.pop(0)
            _TTS_AUDIO_CACHE.pop(old, None)
    # Primary disk tier only; secondary = operator's manual domain.
    ext = _tts_ext_for(value)
    if not ext:
        return
    try:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
        tmp = os.path.join(TTS_CACHE_DIR, f".{key}{ext}.tmp")
        final = os.path.join(TTS_CACHE_DIR, key + ext)
        if not os.path.isfile(final):
            with open(tmp, "wb") as f:
                f.write(value)
            os.replace(tmp, final)
        _tts_prune_disk_cache()
    except OSError as e:
        print(f"[tts] disk cache write skipped: {e}")


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------


def _split_speech_chunks(text, max_chunk=TTS_CHUNK_CHARS):
    """Split clean speech text into sentence-bounded chunks for synthesis.

    Splits on sentence terminators (danda ।, period, question mark,
    exclamation, newline). A single sentence longer than max_chunk is
    split on whitespace words so no chunk exceeds max_chunk.
    """
    if not text:
        return []
    if len(text) <= max_chunk:
        return [text]

    raw_sentences = _re.split(r"([।\.!\?\n]+)", text)
    sentences = []
    i = 0
    while i < len(raw_sentences):
        s = raw_sentences[i]
        punct = raw_sentences[i + 1] if i + 1 < len(raw_sentences) else ""
        combined = (s + punct).strip()
        if combined:
            sentences.append(combined)
        i += 2

    chunks = []
    current = []
    curr_len = 0
    for s in sentences:
        if len(s) > max_chunk:
            if current:
                chunks.append(" ".join(current))
                current = []
                curr_len = 0
            words = s.split()
            w_curr = []
            w_len = 0
            for w in words:
                if w_len + len(w) + (1 if w_curr else 0) > max_chunk:
                    if w_curr:
                        chunks.append(" ".join(w_curr))
                    w_curr = [w]
                    w_len = len(w)
                else:
                    w_curr.append(w)
                    w_len += len(w) + (1 if len(w_curr) > 1 else 0)
            if w_curr:
                chunks.append(" ".join(w_curr))
            continue

        add_len = len(s) + (1 if current else 0)
        if curr_len + add_len > max_chunk:
            chunks.append(" ".join(current))
            current = [s]
            curr_len = len(s)
        else:
            current.append(s)
            curr_len += add_len
    if current:
        chunks.append(" ".join(current))
    return chunks or [text]


def _concat_wav_blobs(wav_blobs):
    """Concatenate multiple single-channel WAV byte strings into one.

    Extracts raw PCM frames from each WAV file and repackages them with
    the first chunk's sample rate, channel count, and sample width.
    """
    if not wav_blobs:
        return b""
    if len(wav_blobs) == 1:
        return wav_blobs[0]

    frames = []
    rate = 22050
    nchannels = 1
    sampwidth = 2
    for i, blob in enumerate(wav_blobs):
        try:
            with _wave.open(_io.BytesIO(blob), "rb") as r:
                if i == 0:
                    rate = r.getframerate()
                    nchannels = r.getnchannels()
                    sampwidth = r.getsampwidth()
                frames.append(r.readframes(r.getnframes()))
        except Exception as e:
            print(f"[tts] concat_wav error on chunk {i}: {e}")
    out = _io.BytesIO()
    with _wave.open(out, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))
    return out.getvalue()


# ---------------------------------------------------------------------------
# Markdown / text cleanup
# ---------------------------------------------------------------------------


def markdown_to_speech_text(text):
    """Convert chat markdown into speakable plain text.

    Strips formatting symbols (so Piper doesn't read "asterisk asterisk"
    or "colon"), drops code blocks/images/URLs, and joins blocks with
    sentence pauses. Only touches ASCII markdown syntax, so Bengali,
    Hindi, Telugu, Kannada and Spanish text passes through untouched.
    """
    from html import unescape as _unescape

    if not text:
        return ""
    t = str(text)
    t = _re.sub(r"(?m)^\s*\|?[\s:\-|]+\|?\s*$", " ", t)
    t = _re.sub(r"```.*?```", " ", t, flags=_re.DOTALL)
    t = _re.sub(r"`([^`]*)`", r"\1", t)
    try:
        import markdown as _md
        from bs4 import BeautifulSoup as _Soup

        soup = _Soup(_md.markdown(t), "html.parser")
        for node in soup(["pre", "code", "script", "style", "img", "hr", "table"]):
            if node.name == "table":
                cells = [
                    c.get_text(" ", strip=True)
                    for c in node.find_all(["th", "td"])
                    if c.get_text(" ", strip=True)
                ]
                node.replace_with(". ".join(cells) + ". " if cells else " ")
            else:
                node.decompose()
        for a in soup.find_all("a"):
            a.replace_with(a.get_text(" ", strip=True))
        parts = []
        for el in soup.find_all(
            ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "div"]
        ):
            s = el.get_text(" ", strip=True)
            if s:
                parts.append(s)
        t = ". ".join(parts) if parts else soup.get_text(" ", strip=True)
    except Exception:
        t = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = _re.sub(r"(?m)^#{1,6}\s*", "", t)
        t = _re.sub(r"[*_~]{1,3}", "", t)
    t = _re.sub(r"https?://\S+|www\.\S+", " ", t)
    t = _re.sub(r"<[^>]+>", " ", t)
    t = _re.sub(r"\[NEXT TURN:[^\]]*\]", " ", t, flags=_re.IGNORECASE)
    t = _re.sub(r"\s*:\s*", ", ", t)
    t = _re.sub(r"[|*_~#>`]+", " ", t)
    t = _re.sub(r"([.!?]){2,}", r"\1", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return _unescape(t)


def story_markdown_to_speech_text(text):
    """Clean a story .md file for speech — strips metadata and prose only.

    Extends ``markdown_to_speech_text`` with story-specific rules: removes
    the metadata header block (Task prompt, Genre, For roles, Mediums,
    Language(s)), round attribution lines, ``<small>`` wrappers, verdict
    blocks, and ``<details>`` source-verification blocks.
    """
    if not text:
        return ""

    lines = str(text).split("\n")

    # Strip the metadata header (everything before the first blank line
    # that separates the header from story prose). Known keys that signal
    # we're still in the header block.
    _METADATA_KEYS = (
        "task prompt", "genre", "dynamic", "for roles",
        "mediums", "language", "language(s)",
    )
    out = []
    in_header = True
    for line in lines:
        stripped = line.strip()
        # Skip the metadata header: lines like "**Task prompt:** ..." or "---"
        if in_header:
            lower = stripped.lower().lstrip("*").strip()
            if not stripped or stripped == "---":
                # blank line or hr — still in header until a known key appeared
                continue
            if any(lower.startswith(k) for k in _METADATA_KEYS):
                continue
            # If we see markdown headers or round markers, we're in prose
            if stripped.startswith("<small>") or _re.match(r"_Round\s+\d", stripped):
                continue
            # Otherwise we've left the header
            in_header = False

        # Skip round attribution lines: _Round N · X Turn N_
        if _re.match(r"<small[^>]*>\s*(<em>)?\s*_?Round\s+\d", stripped, _re.IGNORECASE):
            continue
        # Skip lines that are only source-verification blocks
        if stripped.startswith("<details"):
            continue
        # Skip empty lines after round markers (consecutive blanks)
        out.append(stripped)

    t = "\n".join(out)
    # Now run the standard speech cleanup
    return markdown_to_speech_text(t)


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def detect_tts_lang(text, voice=""):
    """Detect the TTS language tag for cleaned speech text."""
    m = _re.match(r"^\s*\[(bn|hi|te|kn|es|en)\]\s*", text)
    if m:
        return m.group(1), text[m.end():].lstrip()
    if voice:
        return "en", text
    bn = len(_re.findall(r"[\u0980-\u09FF]", text))
    hi = len(_re.findall(r"[\u0900-\u097F]", text))
    te = len(_re.findall(r"[\u0C00-\u0C7F]", text))
    kn = len(_re.findall(r"[\u0C80-\u0CFF]", text))
    scores = {"bn": bn, "hi": hi, "te": te, "kn": kn}
    tag = max(scores, key=scores.get)
    if scores[tag] > 0:
        return tag, text
    if _re.search(r"[ñÑ¡¿áéíóúü]", text):
        return "es", text
    if _re.search(
        r"\b(el|la|los|las|una|uno|unos|unas|qué|está|estás|están|para|porque|"
        r"hola|gracias|por favor|buenos|buenas|días|tardes|noches)\b",
        text,
        _re.IGNORECASE,
    ):
        return "es", text
    return "en", text


# Legacy alias used by api.py callers.
_detect_tts_lang = detect_tts_lang


# ---------------------------------------------------------------------------
# Piper synthesis (thread-safe: locked + one voice load)
# ---------------------------------------------------------------------------


def _build_words_from_alignments(alignments, text, sample_rate):
    """Convert phoneme alignments into word-level boundaries.

    Given a list of ``(start_sample, end_sample, phoneme)`` tuples and the
    original text (space-separated words), returns a list of
    ``{w: str, s: float, e: float}`` dicts with start/end times in seconds.
    """
    if not alignments or not text:
        return []

    raw_words = text.split()
    if not raw_words:
        return []

    words = []
    phoneme_idx = 0

    for word in raw_words:
        if phoneme_idx >= len(alignments):
            words.append({"w": word, "s": 0, "e": 0})
            continue

        # Consume phonemes for this word until we hit a space phoneme
        word_start_sample = alignments[phoneme_idx][0]
        last_sample = alignments[phoneme_idx][1]
        phoneme_idx += 1

        while phoneme_idx < len(alignments):
            phoneme = alignments[phoneme_idx][2]
            if phoneme in (" ", "sil", "spn", ""):
                break
            last_sample = alignments[phoneme_idx][1]
            phoneme_idx += 1

        s = word_start_sample / sample_rate
        e = last_sample / sample_rate
        words.append({"w": word, "s": round(s, 3), "e": round(e, 3)})

    return words


def synthesize_piper_wav(tag, text):
    """Synthesize WAV bytes with the cached Piper voice (thread-safe).

    Returns ``(wav_bytes, word_boundaries)`` where word_boundaries is a
    list of ``{w, s, e}`` dicts (word, start_seconds, end_seconds).
    """
    import piper as _piper

    with _PIPER_LOCK:
        pv = _PIPER_VOICES.get(tag)
        if pv is None:
            onnx_path = PIPER_VOICES[tag]
            if not os.path.isfile(onnx_path):
                raise FileNotFoundError(f"Piper voice missing: {onnx_path}")
            print(f"[tts] Loading Piper voice '{tag}' ...")
            pv = _piper.PiperVoice.load(onnx_path, config_path=onnx_path + ".json", include_alignments=True)
            _PIPER_VOICES[tag] = pv
        print(f"[tts] Piper {tag}: synthesizing {len(text)} chars")
        rate = 22050
        frames = []
        all_alignments = []
        sample_offset = 0
        for chunk in pv.synthesize(text, include_alignments=True):
            try:
                rate = int(chunk.sample_rate)
            except Exception:
                pass
            int16 = (chunk.audio_float_array * 32767).clip(-32768, 32767).astype("<i2")
            frames.append(int16.tobytes())
            if chunk.phoneme_alignments:
                pos = sample_offset
                for a in chunk.phoneme_alignments:
                    all_alignments.append((pos, pos + a.num_samples, a.phoneme))
                    pos += a.num_samples
            sample_offset += len(chunk.audio_float_array)

        # Build word boundaries from phoneme alignments + original text
        words = _build_words_from_alignments(all_alignments, text, rate)

        wav_io = _io.BytesIO()
        with _wave.open(wav_io, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(b"".join(frames))
        return wav_io.getvalue(), words


# Legacy alias used by api.py callers.
_synthesize_piper_wav = synthesize_piper_wav


# ---------------------------------------------------------------------------
# Main synthesis entry point (chunked, cached, both Piper and edge-tts)
# ---------------------------------------------------------------------------


def tts_synthesize(raw_text, voice="", max_chars=TTS_MAX_CHARS, cleaner=markdown_to_speech_text):
    """Clean, detect language and synthesize speech (chunked).

    Shared by all TTS endpoints:
    - authenticated ``/api/tts``: max_chars=8000, cleaner=markdown_to_speech_text
    - public share endpoint: max_chars=8000, cleaner=markdown_to_speech_text
    - story pages: max_chars=7500, cleaner=story_markdown_to_speech_text

    Long text is split into sentence-bounded chunks of <= TTS_CHUNK_CHARS
    each, synthesized or loaded from cache per chunk, and concatenated into
    a single audio blob.

    Returns ``(audio_b64, mime_type)``.
    Raises ``ValueError`` when there is nothing speakable.
    """
    text = cleaner(raw_text)
    tag, text = detect_tts_lang(text, voice)
    if not text:
        raise ValueError("No speakable text found")
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)[0]
        text = cut or text[:max_chars]

    chunks = _split_speech_chunks(text, max_chunk=TTS_CHUNK_CHARS)
    total_chunks = len(chunks)

    if tag in PIPER_VOICES:
        wav_parts = []
        all_words = []
        cumulative_s = 0.0
        for idx, chunk in enumerate(chunks):
            key = hashlib.sha256(f"piper:{tag}:{chunk}".encode("utf-8")).hexdigest()
            part = _tts_cache_get(key)
            if part is None:
                wav_bytes, words = synthesize_piper_wav(tag, chunk)
                part = wav_bytes
                _tts_cache_put(key, part)
                # Words saved per-chunk; adjust to absolute offsets
                chunk_duration_s = words[-1]["e"] if words else 0
                for w in words:
                    all_words.append({
                        "w": w["w"],
                        "s": round(cumulative_s + w["s"], 3),
                        "e": round(cumulative_s + w["e"], 3),
                    })
                cumulative_s += chunk_duration_s
                _tts_words_cache_put(key, words)
            else:
                if total_chunks > 1:
                    print(f"[tts] Piper {tag} chunk {idx+1}/{total_chunks}: cache hit ({len(chunk)} chars)")
                else:
                    print(f"[tts] Piper {tag}: cache hit ({len(chunk)} chars)")
                # Load words from cache
                cached_words = _tts_words_cache_get(key)
                if cached_words:
                    chunk_duration_s = cached_words[-1]["e"] if cached_words else 0
                    for w in cached_words:
                        all_words.append({
                            "w": w["w"],
                            "s": round(cumulative_s + w["s"], 3),
                            "e": round(cumulative_s + w["e"], 3),
                        })
                    cumulative_s += chunk_duration_s
            wav_parts.append(part)
        combined_wav = _concat_wav_blobs(wav_parts)
        # Save full word list
        combined_key = hashlib.sha256(f"piper:{tag}:{text}".encode("utf-8")).hexdigest()
        _tts_words_cache_put(combined_key, all_words)
        return base64.b64encode(combined_wav).decode(), "audio/wav", all_words

    import asyncio, edge_tts

    edge_voice = voice or EDGE_VOICES.get(tag, "en-US-AriaNeural")
    mp3_parts = []
    all_words = []
    cumulative_ms = 0.0

    for idx, chunk in enumerate(chunks):
        key = hashlib.sha256(f"edge:{edge_voice}:{chunk}".encode("utf-8")).hexdigest()
        part = _tts_cache_get(key)
        if part is None:
            if total_chunks > 1:
                print(f"[tts] edge-tts {tag} ({edge_voice}) chunk {idx+1}/{total_chunks}: {len(chunk)} chars")
            else:
                print(f"[tts] edge-tts {tag} ({edge_voice}): {len(chunk)} chars")
            communicate = edge_tts.Communicate(chunk, edge_voice, boundary="WordBoundary")
            mp3_data = bytearray()
            chunk_words = []

            async def _gen():
                async for chunk_piece in communicate.stream():
                    if chunk_piece["type"] == "audio":
                        mp3_data.extend(chunk_piece["data"])
                    elif chunk_piece["type"] == "WordBoundary":
                        # offset is in 100ns ticks, duration same
                        off_ticks = chunk_piece.get("offset", 0)
                        dur_ticks = chunk_piece.get("duration", 0)
                        word_text = chunk_piece.get("text", "")
                        s_ms = off_ticks / 10000.0
                        e_ms = (off_ticks + dur_ticks) / 10000.0
                        chunk_words.append({
                            "w": word_text,
                            "s_ms": round(s_ms, 1),
                            "e_ms": round(e_ms, 1),
                        })

            asyncio.run(_gen())
            part = bytes(mp3_data)
            _tts_cache_put(key, part)

            # Adjust word timings to absolute offsets and accumulate
            chunk_duration_ms = chunk_words[-1]["e_ms"] if chunk_words else 0
            for w in chunk_words:
                all_words.append({
                    "w": w["w"],
                    "s": round((cumulative_ms + w["s_ms"]) / 1000.0, 3),
                    "e": round((cumulative_ms + w["e_ms"]) / 1000.0, 3),
                })
            cumulative_ms += chunk_duration_ms
        else:
            if total_chunks > 1:
                print(f"[tts] edge-tts {tag} chunk {idx+1}/{total_chunks}: cache hit ({len(chunk)} chars)")
            else:
                print(f"[tts] edge-tts {tag}: cache hit ({len(chunk)} chars)")
            # Load per-chunk words from cache and accumulate
            cached_words = _tts_words_cache_get(key)
            if cached_words:
                chunk_duration_ms = cached_words[-1]["e"] * 1000 if cached_words else 0
                for w in cached_words:
                    all_words.append({
                        "w": w["w"],
                        "s": round(cumulative_ms / 1000.0 + w["s"], 3),
                        "e": round(cumulative_ms / 1000.0 + w["e"], 3),
                    })
                cumulative_ms += chunk_duration_ms
        mp3_parts.append(part)
    combined_mp3 = b"".join(mp3_parts)

    # Always save full word list under the combined cache key
    if all_words:
        combined_key = hashlib.sha256(f"edge:{edge_voice}:{text}".encode("utf-8")).hexdigest()
        _tts_words_cache_put(combined_key, all_words)

    return base64.b64encode(combined_mp3).decode(), "audio/mpeg", all_words


# ---------------------------------------------------------------------------
# Public API for word boundaries (called by api.py endpoints)
# ---------------------------------------------------------------------------

def get_tts_words(raw_text, voice="", max_chars=TTS_MAX_CHARS, cleaner=markdown_to_speech_text):
    """Return word boundaries for the last synthesis of *raw_text*.

    Re-runs the same cleanup/detect/truncate logic to compute the exact
    cache key, then looks up the cached words JSON.
    """
    text = cleaner(raw_text)
    tag, text = detect_tts_lang(text, voice)
    if not text:
        return []
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)[0]
        text = cut or text[:max_chars]

    chunks = _split_speech_chunks(text, max_chunk=TTS_CHUNK_CHARS)
    if not chunks:
        return []

    # The combined cache key is the full (cleaned) text
    combined_key = hashlib.sha256(
        (f"piper:{tag}:{text}" if tag in PIPER_VOICES else
         f"edge:{EDGE_VOICES.get(tag, 'en-US-AriaNeural')}:{text}")
        .encode("utf-8")
    ).hexdigest()
    words = _tts_words_cache_get(combined_key)
    if words:
        return words

    # Fallback: combine per-chunk words (may be slightly less accurate)
    cumulative_s = 0.0
    all_words = []
    for chunk in chunks:
        chunk_key = hashlib.sha256(
            (f"piper:{tag}:{chunk}" if tag in PIPER_VOICES else
             f"edge:{EDGE_VOICES.get(tag, 'en-US-AriaNeural')}:{chunk}")
            .encode("utf-8")
        ).hexdigest()
        chunk_words = _tts_words_cache_get(chunk_key)
        if chunk_words:
            for w in chunk_words:
                all_words.append({
                    "w": w["w"],
                    "s": round(cumulative_s + w["s"], 3),
                    "e": round(cumulative_s + w["e"], 3),
                })
            cumulative_s += chunk_words[-1]["e"] if chunk_words else 0
    return all_words


# Legacy alias used by api.py callers.
_tts_synthesize = tts_synthesize


# ---------------------------------------------------------------------------
# Word boundary cache (JSON alongside audio files)
# ---------------------------------------------------------------------------

def _tts_words_cache_put(key, words):
    """Save word boundaries JSON to the primary cache dir."""
    import json as _json

    if not words:
        return
    try:
        os.makedirs(TTS_CACHE_DIR, exist_ok=True)
        path = os.path.join(TTS_CACHE_DIR, f"{key}.words.json")
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(words, f, ensure_ascii=False)
    except OSError as e:
        print(f"[tts] words cache write skipped: {e}")


def _tts_words_cache_get(key):
    """Load word boundaries from cache (primary, then secondary)."""
    import json as _json

    for base in (TTS_CACHE_DIR, TTS_CACHE_SECONDARY_DIR or None):
        if not base:
            continue
        try:
            path = os.path.join(base, f"{key}.words.json")
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    return _json.load(f)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Cache key derivation (for GC)
# ---------------------------------------------------------------------------


def tts_keys_for_raw_text(raw_text, voice="", max_chars=TTS_MAX_CHARS):
    """Compute all possible TTS cache keys for a given raw message text.

    Replicates the normalization, language detection, truncation and
    chunking from ``tts_synthesize`` so the exact cache keys can be
    determined without executing any synthesis.
    """
    text = markdown_to_speech_text(raw_text)
    tag, text = detect_tts_lang(text, voice)
    if not text:
        return set()
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)[0]
        text = cut or text[:max_chars]

    chunks = _split_speech_chunks(text, max_chunk=TTS_CHUNK_CHARS)
    keys = set()
    if tag in PIPER_VOICES:
        for chunk in chunks:
            keys.add(hashlib.sha256(f"piper:{tag}:{chunk}".encode("utf-8")).hexdigest())
    else:
        edge_voice = voice or EDGE_VOICES.get(tag, "en-US-AriaNeural")
        for chunk in chunks:
            keys.add(hashlib.sha256(f"edge:{edge_voice}:{chunk}".encode("utf-8")).hexdigest())
    return keys


# Legacy alias used by api.py callers.
_tts_keys_for_raw_text = tts_keys_for_raw_text


# ---------------------------------------------------------------------------
# Cache key derivation for story pages (uses story-specific cleaner)
# ---------------------------------------------------------------------------


def tts_keys_for_story_text(raw_text, max_chars=TTS_MAX_CHARS):
    """Like ``tts_keys_for_raw_text`` but for story .md content.

    Uses ``story_markdown_to_speech_text`` to strip metadata/header
    before key derivation, so story cache keys match exactly what the
    story-page player would synthesize.
    """
    text = story_markdown_to_speech_text(raw_text)
    tag, text = detect_tts_lang(text)
    if not text:
        return set()
    if len(text) > max_chars:
        cut = text[:max_chars].rsplit(" ", 1)[0]
        text = cut or text[:max_chars]

    chunks = _split_speech_chunks(text, max_chunk=TTS_CHUNK_CHARS)
    keys = set()
    if tag in PIPER_VOICES:
        for chunk in chunks:
            keys.add(hashlib.sha256(f"piper:{tag}:{chunk}".encode("utf-8")).hexdigest())
    else:
        edge_voice = EDGE_VOICES.get(tag, "en-US-AriaNeural")
        for chunk in chunks:
            keys.add(hashlib.sha256(f"edge:{edge_voice}:{chunk}".encode("utf-8")).hexdigest())
    return keys


# ---------------------------------------------------------------------------
# Two-tiered deletion
# ---------------------------------------------------------------------------


def delete_tts_cache_key(key):
    """Two-tier deletion of a cached TTS audio key.

    Checks primary directory first; if file exists, deletes it. If not in
    primary, checks secondary directory and deletes if present. Also removes
    from in-memory cache.
    """
    with _PIPER_LOCK:
        _TTS_AUDIO_CACHE.pop(key, None)
        if key in _TTS_AUDIO_CACHE_ORDER:
            try:
                _TTS_AUDIO_CACHE_ORDER.remove(key)
            except ValueError:
                pass

    for ext in (".wav", ".mp3"):
        primary_file = os.path.join(TTS_CACHE_DIR, key + ext)
        if os.path.isfile(primary_file):
            try:
                os.remove(primary_file)
                print(f"[tts] Deleted primary cache file: {primary_file}")
                continue
            except OSError as e:
                print(f"[tts] Error removing primary file {primary_file}: {e}")

        if TTS_CACHE_SECONDARY_DIR:
            secondary_file = os.path.join(TTS_CACHE_SECONDARY_DIR, key + ext)
            if os.path.isfile(secondary_file):
                try:
                    os.remove(secondary_file)
                    print(f"[tts] Deleted secondary cache file: {secondary_file}")
                except OSError as e:
                    print(f"[tts] Error removing secondary file {secondary_file}: {e}")


# Legacy alias used by api.py callers.
_delete_tts_cache_key = delete_tts_cache_key


# ---------------------------------------------------------------------------
# Share-related helpers (kept here so both api.py and markdown_hosting
# can compute protected/owned TTS keys)
# ---------------------------------------------------------------------------


def share_message_text(rec):
    """Extract speakable text from a share snapshot record."""
    msg = (rec or {}).get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


# Legacy alias.
_share_message_text = share_message_text


def purge_share_tts_audio(snapshot_message, keep_protected_keys):
    """Purge TTS cache keys for a revoked share if not referenced elsewhere."""
    raw_c = share_message_text({"message": snapshot_message})
    if not raw_c:
        return
    keys = tts_keys_for_raw_text(raw_text=raw_c, max_chars=TTS_MAX_CHARS) | tts_keys_for_raw_text(raw_text=raw_c, max_chars=TTS_MAX_CHARS_PUBLIC)
    for k in keys:
        if k not in keep_protected_keys:
            delete_tts_cache_key(k)
