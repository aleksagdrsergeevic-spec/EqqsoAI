"""Talking to upstream providers: target selection, retries, fallback, SSE relay.

Free tiers answer 429 a lot. A gateway that returns that to the agent just
breaks the agent's run, so each public model has a list of targets and the
next one is tried before the request is failed.
"""

import asyncio
import json
import logging
import time

import httpx

from .config import DROP_PARAMS, MODEL_ROUTES, provider_api_key, resolve
from .anthropic_upstream import from_anthropic, to_anthropic
from .errors import GatewayError

log = logging.getLogger("gateway")

TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

# Worth trying again: rate limits, overload, transient server errors.
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 529}
ATTEMPTS_PER_TARGET = 2
BACKOFF = (0.5, 1.5)

# Upstream 4xx that mean "the gateway is misconfigured or knows nothing about
# this model", not "your request is wrong" — those become 502 so the client
# does not go looking for a problem in its own payload.
GATEWAY_FAULT_STATUSES = {401, 403, 404, 405}


def _targets(public_name: str):
    resolved = resolve(public_name)
    if not resolved:
        available = ", ".join(sorted(MODEL_ROUTES))
        raise GatewayError(
            404,
            f"Unknown model '{public_name}'. Available: {available}",
            code="model_not_found",
            param="model",
        )

    name, candidates = resolved
    targets = []
    missing = []
    for provider_name, cfg, upstream_model in candidates:
        key = provider_api_key(cfg)
        if not key:
            missing.append(cfg["key_env"])
            continue
        targets.append({
            "provider": provider_name,
            "cfg": cfg,
            "url": cfg["base_url"].rstrip("/") + ("/messages" if cfg.get("protocol") == "anthropic" else "/chat/completions"),
            "headers": _headers(cfg, key),
            "model": upstream_model,
        })

    if not targets:
        raise GatewayError(
            503,
            f"'{name}' needs an upstream key that is not set: {', '.join(sorted(set(missing)))}",
            type="api_error",
            code="provider_not_configured",
        )
    return name, targets


def _body(payload: dict, upstream_model: str, cfg: dict) -> dict:
    if cfg.get("protocol") == "anthropic":
        body = to_anthropic(payload)
        body["model"] = upstream_model
        return body
    drop = set(DROP_PARAMS) | set(cfg.get("drop_params", ()))
    body = {k: v for k, v in payload.items() if k not in drop}
    body["model"] = upstream_model
    return body


def _headers(cfg: dict, key: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        **cfg.get("extra_headers", {}),
    }
    if cfg.get("protocol") == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _upstream_error(provider_name: str, resp: httpx.Response, text: str) -> GatewayError:
    status = resp.status_code
    detail = _detail(text) or resp.reason_phrase
    message = f"{provider_name} ({status}): {detail}"

    if status in GATEWAY_FAULT_STATUSES:
        return GatewayError(502, message, type="api_error", code="upstream_error")
    if status == 429:
        return GatewayError(429, message, type="rate_limit_error", code="upstream_rate_limited")
    if status in (400, 413, 422):
        # Genuinely about the request: bad tool schema, context too long, ...
        return GatewayError(400, message, code="upstream_rejected_request")
    return GatewayError(502, message, type="api_error", code="upstream_error")


def _detail(text: str) -> str:
    """Pull the human-readable part out of an upstream error body."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except ValueError:
        return text[:800]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            for field in ("message", "detail", "code"):
                if isinstance(error.get(field), str):
                    return error[field][:800]
        if isinstance(error, str):
            return error[:800]
        for field in ("message", "detail"):
            if isinstance(data.get(field), str):
                return data[field][:800]
    return text[:800]


def _delay(attempt: int, retry_after: str) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 5.0)
        except ValueError:
            pass
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def _parse(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise GatewayError(
            502,
            f"Upstream returned a non-JSON body: {resp.text[:800]}",
            type="api_error",
            code="upstream_bad_response",
        )
    if not isinstance(data, dict):
        raise GatewayError(
            502,
            f"Upstream returned an unexpected body: {resp.text[:800]}",
            type="api_error",
            code="upstream_bad_response",
        )
    return data


async def complete(public_name: str, payload: dict) -> dict:
    """Non-streaming call, walking the target list until one answers."""
    name, targets = _targets(public_name)
    last = None

    for target in targets:
        provider_name = target["provider"]
        cfg = target["cfg"]
        url = target["url"]
        headers = target["headers"]
        upstream_model = target["model"]
        body = _body(payload, upstream_model, cfg)
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in range(ATTEMPTS_PER_TARGET):
                started = time.monotonic()
                try:
                    resp = await client.post(url, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    last = GatewayError(
                        502,
                        f"{provider_name} unreachable: {exc.__class__.__name__}",
                        type="api_error",
                        code="upstream_unreachable",
                    )
                    log.warning("%s: %s", provider_name, last.message)
                    break

                elapsed = int((time.monotonic() - started) * 1000)
                if resp.status_code == 200:
                    log.info("ok model=%s provider=%s ms=%d", name, provider_name, elapsed)
                    data = _parse(resp)
                    if cfg.get("protocol") == "anthropic":
                        return from_anthropic(data, name)
                    return data

                last = _upstream_error(provider_name, resp, resp.text)
                log.info("fail model=%s provider=%s status=%s ms=%d", name, provider_name, resp.status_code, elapsed)
                if resp.status_code not in RETRY_STATUSES:
                    raise last
                await asyncio.sleep(_delay(attempt, resp.headers.get("retry-after", "")))

    raise last or GatewayError(502, "No upstream provider answered", type="api_error")


async def open_stream(public_name: str, payload: dict):
    """Streaming call. Returns (client, response) with the body still unread.

    The caller owns both and must close them; `relay` does that.
    """
    name, targets = _targets(public_name)
    last = None

    for target in targets:
        provider_name = target["provider"]
        cfg = target["cfg"]
        url = target["url"]
        headers = target["headers"]
        upstream_model = target["model"]
        body = _body(payload, upstream_model, cfg)
        client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            for attempt in range(ATTEMPTS_PER_TARGET):
                try:
                    request = client.build_request("POST", url, headers=headers, json=body)
                    resp = await client.send(request, stream=True)
                except httpx.HTTPError as exc:
                    last = GatewayError(
                        502,
                        f"{provider_name} unreachable: {exc.__class__.__name__}",
                        type="api_error",
                        code="upstream_unreachable",
                    )
                    log.warning("%s: %s", provider_name, last.message)
                    break

                if resp.status_code == 200:
                    log.info("stream model=%s provider=%s", name, provider_name)
                    if cfg.get("protocol") == "anthropic":
                        return await _open_anthropic_stream(client, resp, name)
                    return client, resp

                # The status arrives before the body, so a failing provider is
                # still cheap to replace with the next target.
                detail = (await resp.aread()).decode("utf-8", "replace")
                retry_after = resp.headers.get("retry-after", "")
                last = _upstream_error(provider_name, resp, detail)
                await resp.aclose()
                log.info("fail model=%s provider=%s status=%s", name, provider_name, resp.status_code)
                if resp.status_code not in RETRY_STATUSES:
                    raise last
                await asyncio.sleep(_delay(attempt, retry_after))
        except BaseException:
            await client.aclose()
            raise
        await client.aclose()

    raise last or GatewayError(502, "No upstream provider answered", type="api_error")


ANTHROPIC_STOP_REASONS = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _sse(payload) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n"


class _AnthropicToOpenAIStream:
    """Re-emits an Anthropic SSE stream as OpenAI chat chunks.

    Everything downstream (the OpenAI relay and the Anthropic translation in
    app/anthropic.py) already speaks OpenAI chunks, so a native Anthropic
    upstream is normalised here once instead of in every consumer. Only
    `aiter_bytes` and `aclose` are used by the callers, which is all of
    httpx.Response they touch.
    """

    def __init__(self, resp: httpx.Response, model: str):
        self._resp = resp
        self._model = model
        self._id = "chatcmpl-" + str(int(time.time() * 1000))
        self._created = int(time.time())
        self._buffer = b""
        self._tool_index = 0
        self._blocks = {}

    async def aclose(self):
        await self._resp.aclose()

    def _chunk(self, delta: dict, finish_reason=None, usage=None) -> bytes:
        payload = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage:
            payload["usage"] = usage
        return _sse(payload)

    async def aiter_bytes(self):
        yield self._chunk({"role": "assistant", "content": ""})
        async for event in self._events():
            for out in self._convert(event):
                yield out

    async def _events(self):
        async for raw in self._resp.aiter_bytes():
            self._buffer += raw
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event

    def _convert(self, event: dict):
        kind = event.get("type")

        if kind == "content_block_start":
            block = event.get("content_block") or {}
            index = event.get("index", 0)
            if block.get("type") == "tool_use":
                tool_index = self._tool_index
                self._tool_index += 1
                self._blocks[index] = tool_index
                yield self._chunk({
                    "tool_calls": [{
                        "index": tool_index,
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": ""},
                    }]
                })

        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                yield self._chunk({"content": delta.get("text", "")})
            elif delta.get("type") == "input_json_delta":
                tool_index = self._blocks.get(event.get("index", 0))
                if tool_index is not None:
                    yield self._chunk({
                        "tool_calls": [{"index": tool_index, "function": {"arguments": delta.get("partial_json", "")}}]
                    })

        elif kind == "message_start":
            usage = event.get("message", {}).get("usage") or {}
            if usage:
                yield self._chunk({}, usage={
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                })

        elif kind == "message_delta":
            delta = event.get("delta") or {}
            usage = event.get("usage") or {}
            if usage:
                yield self._chunk({}, usage={
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                })
            yield self._chunk({}, finish_reason=ANTHROPIC_STOP_REASONS.get(delta.get("stop_reason"), "stop"))

        elif kind == "message_stop":
            yield b"data: [DONE]\n\n"

        elif kind == "error":
            error = event.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else str(error)
            yield _sse({"error": {"message": message or "upstream stream error", "type": "api_error"}})


async def _open_anthropic_stream(client: httpx.AsyncClient, resp: httpx.Response, model: str):
    return client, _AnthropicToOpenAIStream(resp, model)


async def iter_sse(resp: httpx.Response, ping_interval: float = 15.0):
    """Yield the upstream SSE bytes, with a comment on every silent pause.

    Free models can take a while to produce the first token, and agents often
    sit behind a proxy that drops idle connections; ": ping" keeps the socket
    warm and is ignored by every SSE parser.
    """
    queue = asyncio.Queue()
    finished = object()

    async def pump():
        try:
            async for chunk in resp.aiter_bytes():
                await queue.put(chunk)
        except Exception as exc:  # noqa: BLE001 - re-raised in the consumer
            await queue.put(exc)
        finally:
            await queue.put(finished)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), ping_interval)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if item is finished:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()


def _sniff(buffer: bytes, meter) -> bytes:
    """Feed streamed OpenAI chunks to the meter; return the unparsed tail.

    The bytes go to the client untouched — this only reads a copy, so metering
    can happen without delaying the stream or buffering the whole answer.
    """
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == b"[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        meter.observe(event)
        for choice in event.get("choices") or []:
            delta = (choice or {}).get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str):
                meter.add_text(text)
    return buffer


async def relay(resp: httpx.Response, client: httpx.AsyncClient, meter=None):
    """Stream the upstream body to the client and close everything on the way out.

    A failure after the headers went out cannot become an HTTP error any more,
    so it is logged and the stream is ended. `meter`, when given, is charged in
    the same place — once, even if the stream breaks.
    """
    buffer = b""
    try:
        async for chunk in iter_sse(resp):
            if meter is not None and not chunk.startswith(b":"):
                buffer = _sniff(buffer + chunk, meter)
            yield chunk
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - headers are already sent
        log.warning("stream broke: %s", exc)
    finally:
        if meter is not None:
            meter.finish()
        await resp.aclose()
        await client.aclose()
