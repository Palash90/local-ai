"""Regression tests for server/features/toolstrip.py.

Covers the dual-emission leak: the model emitting a structured tool call
AND a text-form echo (<|tool_call> pairs, orphan tags from truncation,
bare call:name{...} lines, [Assistant tool call]: transcript echoes).
"""

from server.features.toolstrip import strip_tool_call_text as strip


def test_paired_tags_removed_prose_kept():
    assert strip("hello <|tool_call>call:foo{a:1}<tool_call|> world") == "hello  world"


def test_orphan_opening_tag_truncation():
    assert strip("<|tool_call>call:foo{a:1}") == ""
    assert strip("done <|tool_call>") == "done"


def test_bare_call_payload_line_removed():
    assert strip("draw done\ncall:generate_image{prompt:cat}") == "draw done"


def test_client_transcript_echo_removed():
    assert strip('hi\n[Assistant tool call]: edit({"a":1})\nbye') == "hi\n\nbye"


def test_plain_prose_untouched():
    assert strip("normal prose, no markup (call me maybe)") == "normal prose, no markup (call me maybe)"
    assert strip("") == ""
    assert strip(None) is None
