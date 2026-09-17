"""Regression tests for server/features/toolstrip.py.

Covers the dual-emission leak: the model emitting a structured tool call
AND a text-form echo (<|tool_call> pairs, orphan tags from truncation,
bare call:name{...} lines, [Assistant tool call]: transcript echoes).
"""

from server.features.toolstrip import strip_tool_call_text as strip
from server.features.toolstrip import is_pure_tool_json as pure


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


def test_react_json_echo_removed():
    react = ('{\n  "action": "generate_image",\n'
             '  "action_input": "{\\"aspect_ratio\\": \\"landscape\\"}",\n'
             '  "thought": "The user wants an image."\n}')
    assert strip(react) == ""
    assert strip(react + "\nHere is your image.") == "Here is your image."


def test_result_json_prefix_removed_prose_kept():
    assert strip('{\n  "image_url": "/output/p/a.png"\n}\n\nThe diagram shows GFS.') == "The diagram shows GFS."
    # mid-prose references are kept (the UI may not attach them)
    assert strip('See {"image_url": "/o/a.png"} above.') == 'See {"image_url": "/o/a.png"} above.'


def test_json_lookalikes_untouched():
    assert strip("We took action yesterday.") == "We took action yesterday."
    assert strip('Config {"name": "x", "type": "y"} inline.') == 'Config {"name": "x", "type": "y"} inline.'


def test_pure_tool_json_detector():
    assert pure('{"action": "generate_image", "action_input": "{}"}') is True
    assert pure('{"image_url": "/o/a.png"}') is True
    assert pure('{"a": 1}') is False
    assert pure("plain prose") is False
    assert pure("") is False
