"""Pensieve smoke test (no repo state needed).

Runs a quick end-to-end pass against a throwaway SQLite DB: store blocks,
recall by id, keyword-search, and trim.

Usage:
    python3 -m server.features.pensieve
"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="pensieve_smoke_")

from server.features.pensieve import memory, distill  # noqa: E402
from server.features.pensieve.retrieval import memory_read  # noqa: E402

memory.PENSIEVE_DB = os.path.join(_tmp, "pensieve_smoke.db")

messages = [
    {"role": "system", "content": "SYS"},
    {"role": "user", "content": "Find the melting point of gallium."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "web_search", "arguments": "{}"}}],
    },
    {"role": "tool", "content": '{"results": ["gallium melts at 29.76 C"]}'},
    {"role": "assistant", "content": "Gallium melts around 30 C."},
    {"role": "user", "content": "And bismuth?"},
    {"role": "assistant", "content": "Bismuth melts at 271.4 C."},
]

out = distill.distill_and_store_messages(
    "smoke", messages, mode="gpu",
    estimate_fn=lambda _m: 10 ** 9,
    budget_fn=lambda: 1000,
    keep_recent=1, watermark_frac=0.1,
)
print("Distilled markers:", [m["content"] for m in out if m["role"] == "system"][1:])
print("Rows stored:", memory.count_units("smoke"))
print("By id:", memory_read("smoke", memory_ids=[1])[:120].replace("\n", " / "))
print("By query:", memory_read("smoke", query="gallium melting")[:120].replace("\n", " / "))
memory.trim_old("smoke", max_units=1)
print("After trim, rows:", memory.count_units("smoke"))
print("SMOKE OK")