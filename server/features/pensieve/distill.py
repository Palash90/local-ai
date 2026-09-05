"""Distillation: decide which conversation blocks to archive, replace them with
``[#id]`` markers, and persist the originals verbatim.

Pensieve "Lite" — fully deterministic, no embeddings:

* A **block** is a fused conversation chain that is never split apart: a
  ``user`` message, or an ``assistant`` message; if the assistant carries
  ``tool_calls``, every immediately-following ``role="tool"`` message (one
  result per call, in order) is fused onto the same block up to the next
  user/assistant/system message.
* When the token estimate crosses ``WATERMARK_FRAC`` of the lane's prompt
  budget, the **oldest** blocks (everything beyond the keep-recent window, the
  system message excepted) are archived to SQLite and replaced in the working
  copy by a compact marker the model can use with ``memory_read``.
* The stored session is never mutated — distillation works on copies.
"""

import os

from server.features.pensieve import memory

RELEVANCE_DISTILL = os.environ.get("RELEVANCE_DISTILL", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
RELEVANCE_WATERMARK_FRAC = float(os.environ.get("RELEVANCE_WATERMARK_FRAC", "0.55"))
RELEVANCE_KEEP_RECENT = int(os.environ.get("RELEVANCE_KEEP_RECENT", "6"))
PENSIEVE_MAX_UNITS = int(os.environ.get("PENSIEVE_MAX_UNITS", "200"))
PENSIEVE_BLOCK_MAX_CHARS = int(os.environ.get("PENSIEVE_BLOCK_MAX_CHARS", "12000"))
PENSIEVE_TOPIC_MAX_CHARS = int(os.environ.get("PENSIEVE_TOPIC_MAX_CHARS", "120"))

_ARCHIVABLE_MODES = {"gpu", "cpu"}


def build_blocks(messages):
    """Split ``messages`` into unbreakable blocks.

    Returns a list of dicts: ``{"start", "end", "kind", "msgs"}`` where
    ``kind`` is ``"system"`` (never archived) or ``"normal"``.
    """
    blocks = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        role = msg.get("role")
        if role == "system":
            blocks.append({"start": i, "end": i + 1, "kind": "system", "msgs": [i]})
            i += 1
            continue
        if role == "user":
            blocks.append({"start": i, "end": i + 1, "kind": "normal", "msgs": [i]})
            i += 1
            continue
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            end = i + 1
            msgs = [i]
            if tool_calls:
                j = i + 1
                while j < n and messages[j].get("role") == "tool":
                    msgs.append(j)
                    end = j + 1
                    j += 1
            blocks.append({"start": i, "end": end, "kind": "normal", "msgs": msgs})
            i = end
            continue
        # role == "tool" with no fusing assistant: defensive single-item block.
        blocks.append({"start": i, "end": i + 1, "kind": "normal", "msgs": [i]})
        i += 1
    return blocks


def _block_topic(messages, msgs):
    """Deterministic marker topic: first user message's text, collapsed and
    truncated (no LLM involved)."""
    text = ""
    for idx in msgs:
        content = messages[idx].get("content")
        if messages[idx].get("role") != "user":
            continue
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            text = " ".join(parts)
        break
    if not text:
        names = []
        for idx in msgs:
            for tc in messages[idx].get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name")
                if fn:
                    names.append(fn)
        if names:
            unique = sorted(set(names))
            text = " / ".join(unique) + " result" if len(unique) == 1 else " ".join(unique) + " results"
        else:
            for idx in msgs:
                content = messages[idx].get("content")
                if isinstance(content, str) and content.strip():
                    text = content
                    break
    collapsed = " ".join(text.split())
    return collapsed[:PENSIEVE_TOPIC_MAX_CHARS].strip() or "archived conversation"


def distill_and_store_messages(
    sid,
    messages,
    mode="gpu",
    estimate_fn=None,
    budget_fn=None,
    max_units=PENSIEVE_MAX_UNITS,
    watermark_frac=RELEVANCE_WATERMARK_FRAC,
    keep_recent=RELEVANCE_KEEP_RECENT,
):
    """Archive the oldest blocks when the conversation is getting tight.

    Returns a *new* message list with archived spans replaced by ``[#id]``
    markers, or the original list when nothing qualifies. ``messages`` is never
    mutated and the stored session is never touched.
    """
    if not RELEVANCE_DISTILL or mode not in _ARCHIVABLE_MODES:
        return messages

    if estimate_fn is None:
        from server.features.context import estimate_tokens

        estimate_fn = estimate_tokens
    if budget_fn is None:
        from server.features.state import M

        budget_fn = lambda m=mode: M.prompt_token_budget(m)
    budget = budget_fn()
    if estimate_fn(messages) <= int(budget * watermark_frac):
        return messages

    blocks = build_blocks(messages)
    candidates = [b for b in blocks if b["kind"] != "system"]
    if len(candidates) <= keep_recent:
        return messages

    keep_count, archived = 0, []
    for b in reversed(candidates):
        if keep_count < keep_recent:
            keep_count += 1
            continue
        archived.append(b)
    if not archived:
        return messages

    markers = {}
    for b in reversed(archived):
        raw_msgs = [messages[i] for i in b["msgs"]]
        topic = _block_topic(messages, b["msgs"])
        total_chars = sum(
            len(str(m.get("content", ""))) for m in raw_msgs
        )
        raw_json = memory._serialize(raw_msgs)
        if total_chars > PENSIEVE_BLOCK_MAX_CHARS:
            head_chars = max(800, PENSIEVE_BLOCK_MAX_CHARS // 2)
            raw_json = (
                raw_json[:head_chars]
                + "\n...older archived block truncated...\n"
                + raw_json[-800:]
            )
        block_id = memory.store_unit(sid, topic, raw_json, len(raw_msgs))
        markers[b["start"]] = (
            f"[#{block_id}: {topic} ({len(raw_msgs)} messages)]"
        )

    memory.trim_old(sid, max_units=max_units)

    distilled = []
    for i, msg in enumerate(messages):
        if i in markers:
            distilled.append(
                {"role": "system", "content": markers[i]}
            )
        elif any(b["start"] <= i < b["end"] for b in archived):
            continue
        else:
            distilled.append(msg)
    return distilled