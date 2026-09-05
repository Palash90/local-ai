"""Retrieval side of Pensieve: turn ``memory_read`` requests into text.

Exact-block lookups (``memory_ids``) go straight to the primary keys and are
instant. ``query`` falls back to a deterministic zero-embed keyword search.
All lookups are scoped to the caller's own session (``sid``).
"""

from server.features.pensieve import memory


def _format_block(block):
    body = memory.deserialize(block["raw"])
    lines = []
    for msg in body:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if not isinstance(content, str):
            try:
                content = str(content)
            except Exception:
                content = ""
        if msg.get("tool_calls"):
            content = f"{content} [tool_calls: {len(msg['tool_calls'])}]"
        lines.append(f"<{role}> {content}")
    text = "\n".join(lines)
    return (
        f"### [#{block['block_id']}] {block['topic']} "
        f"({block['n_msgs']} messages, archived {block['created_ts']})\n{text}"
    )


def memory_read(sid, memory_ids=None, query=None, limit=5):
    """Recall archived blocks for ``sid``.

    ``memory_ids`` (exact ``[#id]`` block ids, e.g. from markers in context)
    take precedence if both params are supplied; ``query`` performs a keyword
    search. Never invents ids — missing ids are reported explicitly so the
    model can self-correct with a ``query`` instead.
    """
    ids = _parse_ids(memory_ids)

    if ids:
        found, missing = memory.fetch_blocks(sid, ids)
        parts = [_format_block(b) for b in found]
        if missing:
            parts.append(
                "ARCHIVE NOTE: could not find "
                f"block{'' if len(missing) == 1 else 's'} "
                f"{missing}. Do not guess; use 'query' to search archived "
                "history instead."
            )
        if not found and not parts:
            return "Archived history is empty for this session."
        return "\n\n".join(parts)

    if query and str(query).strip():
        rows = memory.keyword_search(sid, query, limit=int(limit or 5))
        if not rows:
            return (
                f"No archived blocks matched '{query}' in this session. "
                "If the information was never archived, it is not retrievable."
            )
        return "\n\n".join(_format_block(b) for b in rows)

    return (
        "memory_read requires either memory_ids (exact archived block ids "
        "from '[#id]' markers) or a natural-language query to search this "
        "session's archive. Pass one or the other, never both."
    )


def _parse_ids(memory_ids):
    if memory_ids is None:
        return []
    if isinstance(memory_ids, (int, str)):
        try:
            return [int(memory_ids)]
        except (TypeError, ValueError):
            return []
    out = []
    for item in memory_ids:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out