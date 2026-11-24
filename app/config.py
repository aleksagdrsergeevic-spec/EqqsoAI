"""Config and provider definitions for the gateway.

Upstream provider keys come from the environment (.env file):
    GROQ_API_KEY=...
    OPENROUTER_API_KEY=...
    GEMINI_API_KEY=...
    CEREBRAS_API_KEY=...
    MISTRAL_API_KEY=...

MODEL_ROUTES maps a public model name (what clients send as "model") to an
ordered list of targets: the first one is used, the rest are fallbacks for
when it is rate limited or down. Every provider listed here speaks the OpenAI
chat-completions protocol, so the gateway only has to rewrite the model name.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Params that belong to OpenAI's own platform. Other providers reject or
# ignore them, and an unused param should never turn an agent call into a 400.
DROP_PARAMS = (
    "safety_identifier",
    "service_tier",
    "store",
    "metadata",
    "prompt_cache_key",
    "verbosity",
)

PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "extra_headers": {},
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "extra_headers": {},
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        # OpenRouter uses these to attribute traffic; harmless to send.
        "extra_headers": {
            "HTTP-Referer": "https://localhost",
            "X-Title": "Free API Gateway",
        },
    },
    "gemini": {
        # Google's OpenAI-compatible endpoint.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GEMINI_API_KEY",
        "extra_headers": {},
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env": "MISTRAL_API_KEY",
        "extra_headers": {},
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com/v1",
        "key_env": "ANTHROPIC_API_KEY",
        "protocol": "anthropic",
        "extra_headers": {},
    },
}


# public_name -> route
#   targets:        tried in order; the first is used, the rest are fallbacks
#                   for 429s and outages
#   context_length: advertised in /v1/models and in the shop
#   family/label:   how the shop groups and describes the model
#   multiplier:     TKN charged per 1000 tokens of this model
# Free OpenRouter variants: https://openrouter.ai/models?max_price=0
MODEL_ROUTES = {
    # Llama 3.3 70B: the workhorse — reliable tool calling, 128k context.
    "llama-3.3-70b": {
        "targets": [
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
            {"provider": "cerebras", "model": "llama-3.3-70b"},
            {"provider": "openrouter", "model": "meta-llama/llama-3.3-70b-instruct:free"},
        ],
        "context_length": 128000,
        "family": "Llama",
        "label": "Универсальные задачи",
        "multiplier": 2.5,
    },
    # Small and instant: classification, routing, short summaries.
    "llama-3.1-8b": {
        "targets": [
            {"provider": "groq", "model": "llama-3.1-8b-instant"},
            {"provider": "cerebras", "model": "llama3.1-8b"},
        ],
        "context_length": 128000,
        "family": "Llama",
        "label": "Быстрые ответы",
        "multiplier": 1,
    },
    # Reasoning model; the distill writes its thinking into `reasoning_content`.
    "deepseek-r1-70b": {
        "targets": [
            {"provider": "groq", "model": "deepseek-r1-distill-llama-70b"},
            {"provider": "openrouter", "model": "deepseek/deepseek-r1-distill-llama-70b:free"},
        ],
        "context_length": 128000,
        "family": "DeepSeek",
        "label": "Рассуждения с цепочкой мыслей",
        "multiplier": 4,
    },
    "qwen3-32b": {
        "targets": [
            {"provider": "groq", "model": "qwen/qwen3-32b"},
            {"provider": "cerebras", "model": "qwen-3-32b"},
        ],
        "context_length": 131072,
        "family": "Qwen",
        "label": "Анализ и рассуждения",
        "multiplier": 2,
    },
    # Coding-tuned Qwen; the OpenRouter free variant is the fallback.
    "qwen3-coder": {
        "targets": [
            {"provider": "openrouter", "model": "qwen/qwen3-coder:free"},
            {"provider": "groq", "model": "qwen/qwen3-32b"},
        ],
        "context_length": 131072,
        "family": "Qwen",
        "label": "Программирование",
        "multiplier": 3,
    },
    # gpt-oss 120B: strongest free option for multi-step agent loops.
    "gpt-oss-120b": {
        "targets": [
            {"provider": "groq", "model": "openai/gpt-oss-120b"},
            {"provider": "cerebras", "model": "gpt-oss-120b"},
        ],
        "context_length": 131072,
        "family": "GPT",
        "label": "Агентные сценарии · вызов функций",
        "multiplier": 5,
    },
    # Native Anthropic API — this route does not require an OpenAI-compatible
    # upstream; the gateway translates OpenAI chat requests on the fly.
    "claude-sonnet-4-5": {
        "targets": [{"provider": "anthropic", "model": "claude-sonnet-4-5-20250929"}],
        "context_length": 200000,
        "family": "Claude",
        "label": "Универсальные задачи",
        "multiplier": 15,
    },
    "claude-haiku-4-5": {
        "targets": [{"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}],
        "context_length": 200000,
        "family": "Claude",
        "label": "Быстрые ответы",
        "multiplier": 5,
    },
    "claude-opus-4-5": {
        "targets": [{"provider": "anthropic", "model": "claude-opus-4-5-20251101"}],
        "context_length": 200000,
        "family": "Claude",
        "label": "Глубокий анализ",
        "multiplier": 40,
    },
    # Gemini free tier: huge context, native multimodality.
    "gemini-2.5-flash": {
        "targets": [{"provider": "gemini", "model": "gemini-2.5-flash"}],
        "context_length": 1048576,
        "family": "Gemini",
        "label": "Длинный контекст · 1 млн",
        "multiplier": 1.5,
    },
    "gemini-2.5-pro": {
        "targets": [{"provider": "gemini", "model": "gemini-2.5-pro"}],
        "context_length": 1048576,
        "family": "Gemini",
        "label": "Сложные задачи · 1 млн",
        "multiplier": 12,
    },
    # Mistral free tier. Its own naming ("-latest" aliases) rather than dated ids.
    "mistral-small": {
        "targets": [{"provider": "mistral", "model": "mistral-small-latest"}],
        "context_length": 131072,
        "family": "Mistral",
        "label": "Повседневные задачи",
        "multiplier": 1.2,
    },
    "mistral-nemo": {
        "targets": [{"provider": "mistral", "model": "open-mistral-nemo"}],
        "context_length": 131072,
        "family": "Mistral",
        "label": "Быстрый анализ",
        "multiplier": 1,
    },
    # Explicit OpenRouter routes, for when you want that provider specifically.
    "deepseek-chat-free": {
        "targets": [{"provider": "openrouter", "model": "deepseek/deepseek-chat-v3-0324:free"}],
        "context_length": 163840,
        "family": "DeepSeek",
        "label": "Универсальные задачи",
        "multiplier": 3,
    },
    "llama-3.3-70b-free": {
        "targets": [{"provider": "openrouter", "model": "meta-llama/llama-3.3-70b-instruct:free"}],
        "context_length": 131072,
        "family": "Llama",
        "label": "Резервный маршрут",
        "multiplier": 2.5,
    },
}


def catalog() -> list:
    """The shop's model list: what a buyer sees, with price and grouping."""
    models = []
    for name, route in sorted(MODEL_ROUTES.items()):
        models.append(
            {
                "id": name,
                "family": route.get("family") or PROVIDERS[route["targets"][0]["provider"]]["base_url"],
                "label": route.get("label") or "",
                "multiplier": float(route.get("multiplier") or 1),
                "context_length": route.get("context_length") or 0,
                "provider": route["targets"][0]["provider"],
            }
        )
    return models


def multiplier_of(name: str) -> float:
    route = MODEL_ROUTES.get(name) or {}
    return float(route.get("multiplier") or 1)


def available_models() -> set:
    """Names that at least one configured provider can serve right now."""
    names = set()
    for name, route in MODEL_ROUTES.items():
        for target in route["targets"]:
            cfg = PROVIDERS.get(target["provider"])
            if cfg and provider_api_key(cfg):
                names.add(name)
                break
    return names



# Model used when a client asks for something the gateway does not serve.
# Agent tools hard-code names like "claude-sonnet-4-5" or "gpt-4o-mini"; those
# resolve here (see `is_alias`) instead of failing with 404, so the gateway
# works as a drop-in endpoint without editing the agent's config.
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL") or "gpt-oss-120b"

ALIAS_PREFIXES = (
    "chatgpt",
    "claude",
    "gpt-",
    "grok",
    "o1",
    "o3",
    "o4",
    "text-davinci",
)


def is_alias(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in ALIAS_PREFIXES)


def resolve(public_name: str):
    """Return (name, [(provider_name, provider_cfg, upstream_model), ...]) or None."""
    name = (public_name or "").strip()
    if not name:
        return None

    if name not in MODEL_ROUTES and is_alias(name):
        name = DEFAULT_MODEL if DEFAULT_MODEL in MODEL_ROUTES else sorted(MODEL_ROUTES)[0]

    route = MODEL_ROUTES.get(name)
    if not route:
        return None

    targets = []
    for target in route["targets"]:
        cfg = PROVIDERS.get(target["provider"])
        if cfg:
            targets.append((target["provider"], cfg, target["model"]))
    if not targets:
        return None
    return name, targets


def provider_api_key(provider_cfg: dict) -> str:
    return os.environ.get(provider_cfg["key_env"], "").strip()
