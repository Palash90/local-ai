"""OpenAI-compatible adapter for streaming tool calls.

This module provides formatting and streaming utilities for the OpenAI lane,
ensuring tool calls are streamed in the format expected by VS Code and other
OpenAI-compatible clients.

Key format requirements:
- Tool calls must be streamed incrementally (not as complete objects)
- First chunk: id, type, function.name, function.arguments (empty)
- Subsequent chunks: index, function.arguments (delta)
- Finish chunk: separate from last argument chunk
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Callable


def stream_tool_calls(
    write_sse: Callable[[str], bool],
    tool_calls: List[Dict[str, Any]],
    completion_id: str,
    created: int,
    model: str,
    chunk_size: int = 20,
) -> bool:
    """Stream tool_calls in OpenAI-compatible incremental format.
    
    Args:
        write_sse: Function to write SSE lines, returns False if client disconnected
        tool_calls: List of tool call objects from LLM response
        completion_id: The chat completion ID
        created: Timestamp
        model: Model name
        chunk_size: Size of argument chunks for streaming
        
    Returns:
        True if all chunks sent successfully, False if client disconnected
    """
    for i, tc in enumerate(tool_calls):
        tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
        tc_type = tc.get("type", "function")
        func_name = tc.get("function", {}).get("name", "")
        func_args = tc.get("function", {}).get("arguments", "")
        
        # First chunk: metadata (id, type, name) with empty arguments
        first_delta = {
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": i,
                    "id": tc_id,
                    "type": tc_type,
                    "function": {
                        "name": func_name,
                        "arguments": ""
                    }
                }]
            },
            "finish_reason": None
        }
        
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [first_delta]
        }
        
        if not write_sse(f"data: {json.dumps(chunk)}\n\n"):
            return False
        
        # Stream arguments in incremental chunks
        if func_args:
            for j in range(0, len(func_args), chunk_size):
                arg_chunk = func_args[j:j + chunk_size]
                
                arg_delta = {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{
                            "index": i,
                            "function": {
                                "arguments": arg_chunk
                            }
                        }]
                    },
                    "finish_reason": None
                }
                
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [arg_delta]
                }
                
                if not write_sse(f"data: {json.dumps(chunk)}\n\n"):
                    return False
    
    # Final finish chunk (separate from last argument chunk)
    finish_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "tool_calls"
        }]
    }
    
    if not write_sse(f"data: {json.dumps(finish_chunk)}\n\n"):
        return False
    
    return True


def format_tool_calls_for_response(tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Format tool_calls for non-streaming response.
    
    Ensures tool_calls have proper structure for OpenAI API response.
    """
    formatted = []
    for i, tc in enumerate(tool_calls):
        formatted.append({
            "index": i,
            "id": tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
            "type": tc.get("type", "function"),
            "function": {
                "name": tc.get("function", {}).get("name", ""),
                "arguments": tc.get("function", {}).get("arguments", "")
            }
        })
    return formatted


def parse_tool_results_from_session(session: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract tool results from session history.
    
    Returns list of tool result messages that can be sent back to LLM.
    """
    results = []
    for msg in session:
        if msg.get("role") == "tool":
            results.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": msg.get("content", "")
            })
    return results


def create_tool_call_response(
    tool_call_id: str,
    result: str,
) -> Dict[str, Any]:
    """Create a tool result message in OpenAI format.
    
    Args:
        tool_call_id: The ID of the tool call being responded to
        result: The tool execution result
        
    Returns:
        Tool result message dict
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result
    }
