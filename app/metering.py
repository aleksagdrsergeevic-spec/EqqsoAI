"""Token accounting: turns one API call into a charge on the account.

Price rule: `multiplier` is the TKN cost of 1000 tokens of that model, so a
×5 model costs 5 TKN per 1000 tokens. Cached input tokens are billed at half.

Streaming responses only carry `usage` if the upstream sends it, and free tiers
often do not, so the meter also counts the text it passes through and falls back
to ~4 characters per token. That estimate is logged as such.
"""

import logging

from . import db
from .config import multiplier_of

log = logging.getLogger("gateway")

TOKENS_PER_UNIT = 1000.0
CHARS_PER_TOKEN = 4.0


class Meter:
    def __init__(self, user_id: int, model: str):
        self.user_id = user_id
        self.model = model
        self.multiplier = multiplier_of(model)
        self.prompt_tokens = 0.0
        self.completion_tokens = 0.0
        self.cached_tokens = 0.0
        self.characters = 0
        self._charged = False

    # -- observations ------------------------------------------------------

    def observe(self, data: dict) -> None:
        """Read an OpenAI-style `usage` block, from a body or a stream chunk."""
        if not isinstance(data, dict):
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return

        prompt = usage.get("prompt_tokens")
        if prompt is None:
            prompt = usage.get("input_tokens")
        completion = usage.get("completion_tokens")
        if completion is None:
            completion = usage.get("output_tokens")
        if prompt is not None:
            self.prompt_tokens = float(prompt or 0)
        if completion is not None:
            self.completion_tokens = float(completion or 0)

        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            self.cached_tokens = float(details["cached_tokens"] or 0)

    def add_text(self, text: str) -> None:
        if text:
            self.characters += len(text)

    # -- charge ------------------------------------------------------------

    def tokens(self) -> tuple:
        prompt = self.prompt_tokens
        completion = self.completion_tokens
        if not prompt and not completion and self.characters:
            completion = self.characters / CHARS_PER_TOKEN
        return prompt, completion

    def cost(self) -> float:
        prompt, completion = self.tokens()
        cached = min(self.cached_tokens, prompt)
        billed_prompt = (prompt - cached) + cached / 2
        return ((billed_prompt + completion) / TOKENS_PER_UNIT) * self.multiplier

    def finish(self) -> float:
        """Log and charge once. Safe to call from a finally block."""
        if self._charged:
            return 0.0
        self._charged = True

        prompt, completion = self.tokens()
        cost = round(self.cost(), 6)
        if cost <= 0:
            return 0.0
        try:
            db.charge(self.user_id, self.model, prompt, completion, cost)
            log.info(
                "charge user=%s model=%s in=%.0f out=%.0f cost=%.4f TKN",
                self.user_id, self.model, prompt, completion, cost,
            )
        except Exception as exc:  # noqa: BLE001 - never break a finished request
            log.warning("charge failed: %s", exc)
        return cost
