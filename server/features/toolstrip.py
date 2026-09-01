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


def strip_tool_call_text(text):
    """Remove inline tool-call tags the model emits as *text*.

    Such tags are never meaningful to the caller, so drop them wholesale.
    If the content is *only* tool-call spam, return an empty string so the
    caller can detect it (the OpenAI lane then signals a stop rather than
    echoing junk; the chat lanes reject the draft and re-schedule the round).
    """
    if not text:
        return text
    stripped = _TOOL_CALL_TAG_RE.sub("", text).strip()
    return stripped
