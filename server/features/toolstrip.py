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
    return stripped.strip()
