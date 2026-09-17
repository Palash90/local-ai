"""Removal of tool-call markup the model emits as *text* instead of as
structured ``tool_calls`` deltas.

Two consumers:
- the OpenAI lane (``server/openai_api.py``): requests pin ``tools: []`` +
  ``tool_choice: none``, but a model trained to use tools may still leak
  ``<|tool_call|>``-style tags into its content, which is meaningless to an
  OpenAI client;
- the chat lanes (``server/features/orchestration.py``): no-tool rounds whose
  session history still contains past assistant ``tool_calls`` + ``tool``
  results occasionally imitate the tool-call format as plain text (e.g.
  ``<|tool_call>call:foo{...}<tool_call|>`` — often naming a tool that does
  not even exist). The markup is stripped before the reply is judged, stored
  in the session history, or streamed to the UI.
"""

import re

_TOOL_CALL_TAG_RE = re.compile(
    r"<\|?\s*tool_call\s*\|?>\s*(.*?)\s*<\|?\s*tool_call\s*\|?>",
    flags=re.DOTALL | re.IGNORECASE,
)

# Unpaired fragments: stream cut / max_tokens truncation leaves an opening
# tag with no closer (the pair regex above requires both). Also covers the
# tool_response side of the same template family.
_ORPHAN_TAG_RE = re.compile(
    r"<\|?\s*tool_(call|response)\s*\|?>",
    flags=re.IGNORECASE,
)

# Bare payload echo without tags: the jinja template teaches the model the
# shape ``call:<name>{<args>}`` — imitation rounds emit it as plain text
# (single-level {...} args; the template's own args are flat key:value).
_BARE_CALL_RE = re.compile(
    r"(?m)^[^\S\n]*call\s*:\s*[A-Za-z_][\w\-]*\s*\{[^{}]*\}[^\S\n]*$",
)

# Transcript imitation from agentic clients (e.g. opencode history dumps in
# the OpenAI lane): the model echoes ``[Assistant tool call]: ...`` lines.
# Only assistant output is ever passed here — user messages are untouched.
_CLIENT_ECHO_RE = re.compile(
    r"(?m)^[^\S\n]*\[Assistant tool call\]:.*(?:\n[^\S\n]*\{.*)?$",
)

# ReAct-style JSON echo (26b training dialect, not prompt-taught):
# ``{"action": ..., "action_input": ...}``, optionally with "thought".
# action_input is a JSON-ENCODED string (escaped quotes AND raw braces
# inside), so regexes can't bound the object — parsed with
# JSONDecoder.raw_decode instead. Anchored on the action+action_input key
# pair so ordinary prose and code samples are untouched.
_REACT_KEYS = frozenset({"action", "action_input"})

# Pasted tool-result JSON at message start (e.g. '{"image_url":
# "/output/..."}' restated above the prose): the UI renders the card from
# the attachment fields anyway, so drop the JSON prefix, keep the prose.
_RESULT_KEYS = frozenset({"image_url", "music_url", "music_file", "image_file"})


def _splice_json_blocks(text, require_keys, prefix_only=False, require_all=True):
    """Remove JSON objects matching ``require_keys``.

    Uses raw_decode (nesting/escape-correct). With ``prefix_only``, only a
    block starting at the first non-space character is removed. ``require_all``
    needs every key (ReAct pair); otherwise any overlap suffices (result
    JSON carries exactly one artifact key).
    """
    import json as _json
    dec = _json.JSONDecoder()
    out, idx, n = [], 0, len(text)
    while idx < n:
        start = text.find("{", idx)
        if start < 0:
            out.append(text[idx:])
            break
        try:
            obj, end = dec.raw_decode(text, start)
        except Exception:
            out.append(text[idx:])
            break
        keys = set(obj) if isinstance(obj, dict) else set()
        hit = (require_keys <= keys) if require_all else bool(require_keys & keys)
        if hit and (not prefix_only or not text[idx:start].strip()):
            out.append(text[idx:start])
            idx = end
        else:
            out.append(text[idx:start + 1])
            idx = start + 1
    return "".join(out)

# Toolish keys for the pure-JSON fallback in _finalize_task: if a final
# message parses as JSON containing only these, it is machine echo, not
# an answer, and the caller substitutes a caption.
_TOOLISH_KEYS = frozenset({
    "action", "action_input", "thought", "image_url", "music_url",
    "music_file", "image_file", "tool_calls", "function", "arguments",
    "name", "id", "type", "index",
})


def is_pure_tool_json(text):
    """True if text is (only) a JSON object of tool-ish keys — machine echo."""
    if not text or not text.strip().startswith("{"):
        return False
    try:
        import json as _json
        obj = _json.loads(text.strip())
    except Exception:
        return False
    return isinstance(obj, dict) and bool(obj) and set(obj) <= _TOOLISH_KEYS


def strip_tool_call_text(text):
    """Remove inline tool-call tags the model emits as *text*.

    Such tags are never meaningful to the caller, so drop them wholesale.
    If the content is *only* tool-call spam, return an empty string so the
    caller can detect it (the OpenAI lane then signals a stop rather than
    echoing junk; the chat lanes reject the draft and re-schedule the round).
    """
    if not text:
        return text
    stripped = _TOOL_CALL_TAG_RE.sub("", text)
    stripped = _ORPHAN_TAG_RE.sub("", stripped)
    stripped = _BARE_CALL_RE.sub("", stripped)
    stripped = _CLIENT_ECHO_RE.sub("", stripped)
    stripped = _splice_json_blocks(stripped, _REACT_KEYS)
    stripped = _splice_json_blocks(stripped, _RESULT_KEYS, prefix_only=True, require_all=False)
    return stripped.strip()
