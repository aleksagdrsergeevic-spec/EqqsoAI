"""Anthropic Messages API compatibility layer (POST /v1/messages).

Agent tools written for Claude — Claude Code, Cline in Anthropic mode, the
Anthropic SDK — speak this protocol, not OpenAI's. The gateway translates
their requests to chat completions for the free upstreams and translates the
answers back: text, tool use, images, streaming and error envelopes.
"""

import json
import logging
import uuid

from .providers import iter_sse

log = logging.getLogger("gateway")

# OpenAI finish_reason -> Anthropic stop_reason
STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}

ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
    529: "overloaded_error",
}


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def error_body(status_code: int, message: str) -> dict:
    """Anthropic's error envelope. OpenAI's shape stops Claude clients cold."""
    if status_code in ERROR_TYPES:
        kind = ERROR_TYPES[status_code]
    elif status_code >= 500:
        kind = "api_error"
    else:
        kind = "invalid_request_error"
    return {"type": "error", "error": {"type": kind, "message": message}}


# --------------------------------------------------------------------------
# request: Anthropic -> OpenAI
# --------------------------------------------------------------------------


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _image_part(block: dict):
    source = block.get("source") or {}
    if source.get("type") == "base64":
        media_type = source.get("media_type") or "image/png"
        data = source.get("data") or ""
        if not data:
            return None
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
    if source.get("type") == "url" and source.get("url"):
        return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def _assistant_messages(content) -> list:
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]

    texts = []
    tool_calls = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            texts.append(block.get("text", ""))
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id") or "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
        # "thinking" / "redacted_thinking" blocks have no OpenAI equivalent;
        # the upstream reasons on its own.

    text = "".join(texts)
    if not text and not tool_calls:
        return []
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return [message]


def _user_messages(content) -> list:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    parts = []
    tool_messages = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif kind == "image":
            part = _image_part(block)
            if part:
                parts.append(part)
        elif kind == "tool_result":
            result = _text_of(block.get("content"))
            if block.get("is_error"):
                result = f"Error: {result}" if result else "Error"
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": result or "(empty result)",
                }
            )
        # "document" blocks (PDFs) and server-side tool blocks are dropped:
        # no free upstream accepts them and silently losing a PDF is better
        # than a 400 the agent cannot recover from.

    messages = list(tool_messages)
    # Tool results must directly follow the assistant tool_calls message, so
    # they come first; any prose from the same turn follows them.
    if parts:
        if all(part["type"] == "text" for part in parts):
            messages.append({"role": "user", "content": "".join(part["text"] for part in parts)})
        else:
            messages.append({"role": "user", "content": parts})
    return messages


def _tool_choice(choice):
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return None


def to_openai(body: dict) -> dict:
    messages = []

    system = body.get("system")
    if system:
        text = _text_of(system)
        if text:
            messages.append({"role": "system", "content": text})

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            messages.extend(_assistant_messages(message.get("content")))
        else:
            messages.extend(_user_messages(message.get("content")))

    payload = {"model": body.get("model") or "", "messages": messages}

    for field in ("max_tokens", "temperature", "top_p", "stream"):
        if body.get(field) is not None:
            payload[field] = body[field]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]

    tools = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        schema = tool.get("input_schema")
        if not schema:
            # Server-side tools (web_search, ...) have no schema and cannot be
            # forwarded; the model answers without them.
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "parameters": schema,
                },
            }
        )
    if tools:
        payload["tools"] = tools
        choice = _tool_choice(body.get("tool_choice"))
        if choice is not None:
            payload["tool_choice"] = choice

    return payload


# --------------------------------------------------------------------------
# response: OpenAI -> Anthropic
# --------------------------------------------------------------------------


def _json_object(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"raw": str(raw)}
    return value if isinstance(value, dict) else {"value": value}


def from_openai(data: dict, model: str, message_id: str) -> dict:
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}

    blocks = []
    text = message.get("content")
    if isinstance(text, list):
        text = _text_of(text)
    if text:
        blocks.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or "toolu_" + uuid.uuid4().hex[:24],
                "name": function.get("name", ""),
                "input": _json_object(function.get("arguments")),
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    usage = data.get("usage") or {}
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": STOP_REASONS.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


# --------------------------------------------------------------------------
# streaming: OpenAI chunks -> Anthropic events
# --------------------------------------------------------------------------


def _event(name: str, payload: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


class _Stream:
    """Turns OpenAI deltas into the Anthropic event sequence.

    OpenAI sends text and tool arguments incrementally with no notion of
    blocks; Anthropic clients need content_block_start / _delta / _stop around
    each one, in the order the blocks appear.
    """

    def __init__(self, model: str, message_id: str):
        self.model = model
        self.message_id = message_id
        self.next_index = 0
        self.text_index = None
        self.tools = {}  # upstream tool index -> {"block", "id", "name", "args"}
        self.finish_reason = None
        self.usage = {}

    def start(self) -> bytes:
        return _event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )

    def handle(self, chunk: dict) -> list:
        events = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            choice = choice or {}
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}

            text = delta.get("content")
            if text:
                events.extend(self._text(text))
            for call in delta.get("tool_calls") or []:
                events.extend(self._tool(call))
        return events

    def _open(self) -> int:
        index = self.next_index
        self.next_index += 1
        return index

    def _text(self, text: str) -> list:
        events = []
        if self.text_index is None:
            self.text_index = self._open()
            events.append(
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self.text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        events.append(
            _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return events

    def _json_delta(self, index: int, partial: str) -> bytes:
        return _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            },
        )

    def _tool(self, call: dict) -> list:
        upstream_index = call.get("index", 0)
        state = self.tools.get(upstream_index)
        if state is None:
            state = {"block": None, "id": "", "name": "", "args": ""}
            self.tools[upstream_index] = state

        function = call.get("function") or {}
        if call.get("id"):
            state["id"] = call["id"]
        if function.get("name"):
            state["name"] = function["name"]
        fragment = function.get("arguments") or ""

        events = []
        if state["block"] is None:
            if not state["name"]:
                # Hold the arguments until the name arrives: an agent cannot
                # route a tool call it has no name for.
                state["args"] += fragment
                return events
            state["block"] = self._open()
            events.append(
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": state["block"],
                        "content_block": {
                            "type": "tool_use",
                            "id": state["id"] or "toolu_" + uuid.uuid4().hex[:24],
                            "name": state["name"],
                            "input": {},
                        },
                    },
                )
            )
            if state["args"]:
                events.append(self._json_delta(state["block"], state["args"]))
                state["args"] = ""
            if fragment:
                events.append(self._json_delta(state["block"], fragment))
            return events

        if fragment:
            events.append(self._json_delta(state["block"], fragment))
        return events

    def stop(self) -> list:
        events = []
        if self.text_index is not None:
            events.append(_event("content_block_stop", {"type": "content_block_stop", "index": self.text_index}))
        for state in self.tools.values():
            if state["block"] is not None:
                events.append(_event("content_block_stop", {"type": "content_block_stop", "index": state["block"]}))

        events.append(
            _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": STOP_REASONS.get(self.finish_reason, "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": {
                        "input_tokens": int(self.usage.get("prompt_tokens") or 0),
                        "output_tokens": int(self.usage.get("completion_tokens") or 0),
                    },
                },
            )
        )
        events.append(_event("message_stop", {"type": "message_stop"}))
        return events


async def stream_events(resp, client, model: str, message_id: str, meter=None):
    """Relay an upstream OpenAI SSE stream as Anthropic events."""
    session = _Stream(model, message_id)
    buffer = b""
    failed = False

    try:
        yield session.start()

        async for chunk in iter_sse(resp):
            if chunk.startswith(b":"):
                # keepalive comment from providers.iter_sse; already valid SSE
                yield chunk
                continue

            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data:
                    continue
                if data == b"[DONE]":
                    buffer = b""
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if meter is not None:
                    meter.observe(event)
                    for choice in event.get("choices") or []:
                        text = ((choice or {}).get("delta") or {}).get("content")
                        if isinstance(text, str):
                            meter.add_text(text)
                if event.get("error"):
                    message = event["error"]
                    if isinstance(message, dict):
                        message = message.get("message") or json.dumps(message)
                    yield _event("error", error_body(502, str(message)))
                    failed = True
                    break
                for out in session.handle(event):
                    yield out
            if failed:
                break

        if not failed:
            for event in session.stop():
                yield event
    except Exception as exc:  # noqa: BLE001 - headers are already sent
        log.warning("anthropic stream broke: %s", exc)
        yield _event("error", error_body(529, f"Upstream stream failed: {exc}"))
    finally:
        if meter is not None:
            meter.finish()
        await resp.aclose()
        await client.aclose()


def estimate_tokens(body: dict) -> int:
    """Rough token count for /v1/messages/count_tokens (~4 characters each)."""
    chars = (
        len(json.dumps(body.get("messages") or [], ensure_ascii=False))
        + len(json.dumps(body.get("system") or "", ensure_ascii=False))
        + len(json.dumps(body.get("tools") or [], ensure_ascii=False))
    )
    return max(1, chars // 4)
