"""One service: the shop, the cabinet and the OpenAI-compatible API.

    GET  /                          landing page (and the cabinet after login)
    GET  /health                    status and which providers have keys
    GET  /v1/models                 models an agent may ask for
    POST /v1/chat/completions       OpenAI chat, streaming supported
    POST /v1/messages               Anthropic Messages (Claude clients)
    POST /v1/messages/count_tokens  rough token count
    /api/*                          sessions, balance, keys, history
"""

import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import anthropic, db, limits, metering, web
from .auth import resolve_api_key
from .config import DEFAULT_MODEL, MODEL_ROUTES, PROVIDERS, available_models, provider_api_key, resolve
from .errors import GatewayError, install_error_handlers
from .providers import complete, open_stream, relay

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

log = logging.getLogger("gateway")

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

db.init()

app = FastAPI(title="EqqsoAI", version="1.0.0")
install_error_handlers(app)
app.include_router(web.router)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    elapsed = int((time.monotonic() - started) * 1000)
    if request.url.path.startswith("/v1"):
        log.info("%s %s -> %s (%dms)", request.method, request.url.path, response.status_code, elapsed)
    return response


def _configured_providers() -> list:
    return [name for name, cfg in PROVIDERS.items() if provider_api_key(cfg)]


def _authorize(request: Request) -> dict:
    """Who is calling, and may they spend right now."""
    principal = resolve_api_key(request)
    limits.check(principal["key"])
    return principal


def _meter_for(principal: dict, model: str):
    """Meter only real accounts; the operator key is not billed."""
    if principal.get("user_id") is None:
        return None
    priced = resolve(model)
    return metering.Meter(principal["user_id"], priced[0] if priced else model)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise GatewayError(400, "Request body must be valid JSON", code="invalid_json")
    if not isinstance(body, dict):
        raise GatewayError(400, "Request body must be a JSON object", code="invalid_json")
    return body


def _model_of(body: dict) -> str:
    model = body.get("model")
    if not model or not isinstance(model, str):
        raise GatewayError(400, "'model' is required", code="missing_model", param="model")
    return model


@app.get("/")
async def index():
    page = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(page):
        return JSONResponse({"name": "EqqsoAI", "hint": "static/index.html is missing"})
    return FileResponse(page, media_type="text/html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "configured_providers": _configured_providers(),
        "models": len(available_models()),
        "default_model": DEFAULT_MODEL,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    _authorize(request)
    usable = available_models()
    data = []
    for name, route in sorted(MODEL_ROUTES.items()):
        if name not in usable:
            continue
        entry = {
            "id": name,
            "object": "model",
            "created": 0,
            "owned_by": route["targets"][0]["provider"],
        }
        if route.get("context_length"):
            entry["context_length"] = route["context_length"]
        data.append(entry)
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    principal = _authorize(request)
    body = await _json_body(request)
    model = _model_of(body)
    meter = _meter_for(principal, model)

    if body.get("stream"):
        client, resp = await open_stream(model, body)
        return StreamingResponse(
            relay(resp, client, meter),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    result = await complete(model, body)
    if meter:
        meter.observe(result)
        meter.finish()
    return JSONResponse(content=result)


@app.post("/v1/messages")
async def messages(request: Request):
    try:
        principal = _authorize(request)
        body = await _json_body(request)
        model = _model_of(body)
        if not body.get("messages"):
            raise GatewayError(400, "'messages' is required", code="missing_messages", param="messages")

        payload = anthropic.to_openai(body)
        message_id = anthropic.new_message_id()
        meter = _meter_for(principal, model)

        if body.get("stream"):
            client, resp = await open_stream(model, payload)
            return StreamingResponse(
                anthropic.stream_events(resp, client, model, message_id, meter),
                media_type="text/event-stream",
                headers=SSE_HEADERS,
            )

        result = await complete(model, payload)
        if meter:
            meter.observe(result)
            meter.finish()
        return JSONResponse(content=anthropic.from_openai(result, model, message_id))
    except GatewayError as exc:
        return JSONResponse(status_code=exc.status_code, content=anthropic.error_body(exc.status_code, exc.message))


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        _authorize(request)
        body = await _json_body(request)
        return {"input_tokens": anthropic.estimate_tokens(body)}
    except GatewayError as exc:
        return JSONResponse(status_code=exc.status_code, content=anthropic.error_body(exc.status_code, exc.message))
