"""Translate OpenAI Chat Completions requests to the native Anthropic API."""

import json


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def to_anthropic(payload: dict) -> dict:
    messages = []
    system = []
    tools_by_id = {}
    for message in payload.get("messages", []):
        role = message.get("role")
        content = message.get("content")
        if role == "system" or role == "developer":
            text = _content_to_text(content)
            if text:
                system.append(text)
        elif role == "tool":
            messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": _content_to_text(content),
            }]})
        elif role == "assistant" and message.get("tool_calls"):
            blocks = []
            if content:
                blocks.append({"type": "text", "text": _content_to_text(content)})
            for call in message["tool_calls"]:
                function = call.get("function", {})
                call_id = call.get("id") or "toolu_unknown"
                tools_by_id[call_id] = function.get("name", "")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": call_id, "name": function.get("name", ""), "input": args})
            messages.append({"role": "assistant", "content": blocks})
        else:
            messages.append({"role": "assistant" if role == "assistant" else "user", "content": _content_to_text(content)})

    result = {"model": payload.get("model", ""), "messages": messages, "max_tokens": payload.get("max_tokens") or 1024}
    if payload.get("stream"):
        result["stream"] = True
    if system:
        result["system"] = "\n\n".join(system)
    for source, target in (("temperature", "temperature"), ("top_p", "top_p"), ("stop", "stop_sequences")):
        if payload.get(source) is not None:
            result[target] = payload[source]
    if payload.get("tools"):
        result["tools"] = [{
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get("parameters") or {"type": "object", "properties": {}},
        } for tool in payload["tools"] if tool.get("type") == "function"]
    choice = payload.get("tool_choice")
    if choice == "auto":
        result["tool_choice"] = {"type": "auto"}
    elif choice == "required":
        result["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict) and choice.get("type") == "function":
        result["tool_choice"] = {"type": "tool", "name": choice.get("function", {}).get("name", "")}
    return result


def from_anthropic(data: dict, public_model: str) -> dict:
    content = []
    tool_calls = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            content.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)},
            })
    reason = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(data.get("stop_reason"), "stop")
    message = {"role": "assistant", "content": "\n".join(content) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = data.get("usage", {})
    return {
        "id": data.get("id", "chatcmpl-anthropic"),
        "object": "chat.completion",
        "created": 0,
        "model": public_model,
        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
    }
