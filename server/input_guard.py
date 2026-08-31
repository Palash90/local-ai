"""Input guardrail for MCP gateway chat inputs.

Two independent layers:

1. ``is_jailbreak_attempt`` — literal substring filter over inbound user
   text. Flagged inputs are declined locally: nothing is sent upstream, no
   task is created, no session history is touched.
2. ``wrap_user_message`` — everything that passes is forwarded wrapped in
   the SAFETY DIRECTIVES / CRITICAL DIRECTIVE frame with explicit
   ``<user_input> / </user_input>`` XML boundaries, so instructions
   embedded inside user text stay user text and the model is told to answer
   boundary-violating content with a fixed refusal instead of complying.

Layer 1 is intentionally naive (cheap, deterministic substring matching);
novel jailbreak phrasings simply fall through to layer 2.

The LLM safety judge moved out: all judge machinery (prompts, verdict
parsing, candidate selection, ``ensure_judge_ready``, ``_run_judge``,
``llm_classify_harmful*``, ``mcp_output_judge``) now lives in
:mod:`server.features.judge`.

All pattern/prompt files live in a configurable ``SURFACE_ATTACKS_DIR``
(outside this repo) and are read via :mod:`server.features.surface_loader`.
"""

import unicodedata

from server.features.surface_loader import _get_patterns, _get_prompt

GUARDRAIL_DECLINE = "I cannot fulfill this request."
MODEL_REFUSAL = "Request declined."
HARMFUL_DECLINE = (
    "I can't provide instructions for creating weapons, explosives, "
    "incendiaries, or illegal/harmful substances. If you're researching a "
    "legitimate topic (history, safety, policy), I can discuss it in general terms."
)


def _normalize(text: str) -> str:
    """Lowercase and strip diacritics so ASCII patterns match accented text
    (e.g. French "bombe à essence" → "bombe a essence")."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        c for c in decomposed if not unicodedata.combining(c)
    ).lower()


# ── Lazy accessors (used by mcp_gateway + internal functions) ────────────────

def _injection_patterns():
    return _get_patterns("injection_patterns.txt")


def _harmful_request_patterns():
    return _get_patterns("harmful_request_patterns.txt")


def _harmful_output_patterns():
    return _get_patterns("harmful_output_patterns.txt")


def _strict_output_patterns():
    return _get_patterns("strict_output_patterns.txt")


def _safety_frame():
    return _get_prompt("safety_frame.txt")


def is_jailbreak_attempt(message: str) -> bool:
    msg_lower = (message or "").lower()
    return any(pattern in msg_lower for pattern in _injection_patterns())


# ── Harmful-content guardrail ────────────────────────────────────────────────
# Layer 1 (above) blocks known *jailbreak* phrasings. This layer blocks
# explicit requests to manufacture weapons / explosives / illegal harmful
# substances, and — crucially — scans the MODEL'S OWN OUTPUT, because the
# advisory safety frame alone is not reliably obeyed by local LLMs (e.g. they
# will still walk through building a gasoline bomb).
#
# Patterns are intentionally high-signal to limit false positives; they are a
# first line of defence, not a substitute for real content moderation. Tune the
# lists below as needed.


def is_harmful_request(message: str) -> bool:
    """True if the inbound user text is an explicit harmful-manufacture request."""
    msg = _normalize(message)
    return any(pattern in msg for pattern in _harmful_request_patterns())


def is_harmful_content(text: str) -> bool:
    """True if generated text looks like harmful manufacture instructions.

    Scans the model's output for high-signal harmful how-to phrasing. Best
    effort: substring matching, so novel phrasings may slip through, but it
    reliably catches the common compliance cases (e.g. gasoline-bomb walkthroughs).
    """
    txt = _normalize(text)
    return any(pattern in txt for pattern in _harmful_output_patterns())


def is_strict_output_blocked(text: str) -> bool:
    """True if generated text matches strict MCP output prohibition patterns.

    Broader than ``is_harmful_content``: covers sexual, violent, gore, death,
    suicide, murder, illegal content, AND prompt/input/system-prompt leaking.
    Intentionally aggressive — false positives are acceptable because the
    downstream LLM judge is the final arbiter.
    """
    txt = _normalize(text)
    return any(pattern in txt for pattern in _strict_output_patterns())


def wrap_user_message(user_message: str) -> str:
    wrapped = _safety_frame().format(
        decline=GUARDRAIL_DECLINE,
        refusal=MODEL_REFUSAL,
        user_message=user_message if isinstance(user_message, str) else "",
    )
    # Normalise the legacy bracket boundaries to XML tags so the model gets an
    # unambiguous structural boundary around untrusted user text.
    return (
        wrapped.replace("[USER INPUT START]", "<user_input>")
        .replace("[USER INPUT END]", "</user_input>")
        .strip()
    )