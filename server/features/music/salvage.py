"""Recover a generate_music tool call from truncated tool-call JSON.

A model that ignores the score budget writes past MAX_OUTPUT_TOKENS
mid-string; the result is an unterminated `{"score":"...` blob. Dispatch
used to degrade such calls to ``args = {}`` (a featureless "no notes
parsed" with no errors to fix on), and the re-emit loop then died on a
deterministic llama-server 500. Salvage recovers the score instead,
deterministically and bounded:

  1. slice the score string out of the raw args (JSON-escape aware),
  2. unescape it and drop the partial tail line (the string was cut
     mid-line by construction),
  3. strip parser-flagged junk tokens (surgical) or lines (fallback),
     up to a fixed iteration cap,
  4. trim from the tail until the score fits the documented budget
     AND parses playable.

The model's material survives in prefix order — directives and lane
headers at the top are always preserved — and the result flows through
the normal strict render path like any other model-written score.
"""

import re

from server.features.music.parse import parse_score

# Documented model-written budget (music_dsl.txt "HARD BUDGET"). Salvage
# targets it directly so a recovered score can never be a 40-minute
# ramble; the strict render gate (1400) allows a little more slack for
# scores that DID close their JSON.
SALVAGE_MAX_CHARS = 1100

_UNESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}


def _unescape(s):
    return re.sub(r"\\(.)", lambda m: _UNESC.get(m.group(1), m.group(1)), s)


def _score_usable(txt, tempo):
    """Parse-clean AND has events AND any declared pitched lane has real
    notes (mirrors render_score's fail-loud guard so a salvaged result
    can never ship as drums-only mislabeled)."""
    secs, errs, _ = parse_score(txt, tempo)
    if errs or not secs:
        return False
    if not any(s["events"] for s in secs):
        return False
    pitched = [s for s in secs if not s.get("drum")]
    if pitched and not any(e["type"] in ("note", "chord")
                           for s in pitched for e in s["events"]):
        return False
    return True


_LINE_ERR_RE = re.compile(r"line (\d+): (.+)$")
_BAD_TOKEN_RE = re.compile(r"bad token (.+)$")
_WORD_RE = re.compile(r"unknown lane word '([^']*)'")


def _repair(txt, tempo, rounds=40):
    """Strip parser-flagged junk from txt until parse-clean.

    Surgical first (remove just the offending token so lane headers and
    good notes around it survive), whole-line drop as the fallback.
    Bounded: give up (return None) rather than loop forever."""
    for _ in range(rounds):
        _, errs, _ = parse_score(txt, tempo)
        if not errs:
            return txt
        lines = txt.split("\n")
        changed = False
        for e in errs:
            m = _LINE_ERR_RE.match(str(e))
            if not m:
                return None  # error without a line anchor — not repairable
            n, detail = int(m.group(1)), m.group(2)
            if not 1 <= n <= len(lines):
                return None
            tok = None
            tm = _BAD_TOKEN_RE.match(detail)
            if tm:
                tok = tm.group(1).strip().strip("'\"")
            else:
                wm = _WORD_RE.search(detail)
                if wm:
                    tok = wm.group(1)
            if tok:
                new_line, cnt = re.subn(
                    r"(?<!\S)" + re.escape(tok) + r"(?!\S)", "",
                    lines[n - 1], count=1)
                if cnt:
                    lines[n - 1] = new_line.strip()
                    changed = True
                    continue
            # surgical strip impossible (multi-line / structural error):
            # drop the whole offending line.
            if len(lines) > 1:
                lines.pop(n - 1)
                changed = True
        if not changed:
            return None
        txt = "\n".join(lines)
    return None


def salvage_music_args(raw, tempo_default=120):
    """Return generate_music-style args recovered from raw (possibly
    truncated) tool-call arguments, or None if nothing playable remains."""
    if not isinstance(raw, str) or '"score"' not in raw:
        return None
    m = re.search(r'"score"\s*:\s*"((?:[^"\\]|\\.)*)', raw, re.S)
    if not m:
        return None
    score = _unescape(m.group(1))
    tm = re.search(r'"tempo"\s*:\s*(\d{1,5})', raw)
    tempo = int(tm.group(1)) if tm else tempo_default
    if not 20 <= tempo <= 300:
        tempo = tempo_default
    # an @tempo directive inside the score wins (render honors it anyway;
    # this just keeps args["tempo"] consistent with what will play).
    from server.features.music.parse import parse_tempo
    tempo = parse_tempo(score, tempo)
    titm = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)', raw)
    title = _unescape(titm.group(1)) if titm else "music"
    lines = score.split("\n")
    # No unconditional tail-pop: a cut at a line boundary leaves a
    # complete final line (dropping it would lose valid material), and a
    # partial final line is handled by _repair — its junk tokens get
    # stripped surgically, or the line drops as a whole.
    txt = "\n".join(lines).strip("\n")
    if not txt:
        return None
    txt = _repair(txt, tempo)
    if not txt:
        return None
    while len(txt) > SALVAGE_MAX_CHARS:
        ls = txt.split("\n")
        if len(ls) <= 1:
            return None
        ls.pop()
        txt = "\n".join(ls).strip("\n")
    while txt and not _score_usable(txt, tempo):
        ls = txt.split("\n")
        if len(ls) <= 1:
            return None
        ls.pop()
        txt = "\n".join(ls).strip("\n")
    if not txt or not _score_usable(txt, tempo):
        return None
    return {"score": txt, "tempo": tempo, "title": title, "_salvaged": True}
