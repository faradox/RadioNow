"""Per-broadcast cost tracking for LLM tokens and Replicate TTS usage.

Prices are list prices in USD. Entries marked unverified are estimates that
should be checked against the Replicate model pages; override any Replicate
price with REPLICATE_PRICES, e.g.
  REPLICATE_PRICES='{"qwen/qwen3-tts": "per_second:0.000975"}'
"""

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date

logger = logging.getLogger("radionow.costs")

# model -> list of (valid_from or None, input $/1M tokens, output $/1M tokens),
# newest first. Output includes thinking tokens for both providers.
LLM_PRICES = {
    "gemini-4-argon": [(date(2026, 12, 1), 4.0, 20.0), (None, 2.0, 10.0)],
    "gemini-3.8-flash": [(date(2027, 1, 1), 1.5, 7.5), (None, 0.75, 3.75)],
    "claude-opus-5-5": [(None, 4.0, 20.0)],
    "claude-opus-5": [(None, 5.0, 25.0)],
    "claude-opus-4-8": [(None, 5.0, 25.0)],
    "claude-sonnet-5-5": [(None, 2.0, 10.0)],
}

# model -> (pricing spec, verified?)
DEFAULT_REPLICATE_PRICES = {
    # Replicate MiniMax HD page: "$0.10 per 1,000 input tokens", i.e. chars x $0.0001.
    "minimax/speech-2.8-hd": ("per_char:0.0001", True),
    "minimax/speech-2.6-hd": ("per_char:0.0001", True),
    "minimax/voice-cloning": ("per_run:3.00", False),
    "qwen/qwen3-tts": ("per_second:0.001525", False),
}


def _load_replicate_prices() -> dict[str, tuple[str, bool]]:
    prices = dict(DEFAULT_REPLICATE_PRICES)
    raw = os.getenv("REPLICATE_PRICES", "").strip()
    if raw:
        try:
            for model, spec in json.loads(raw).items():
                prices[model] = (str(spec), True)
        except (ValueError, AttributeError) as e:
            logger.warning("Ignoring invalid REPLICATE_PRICES: %s", e)
    return prices


REPLICATE_PRICES = _load_replicate_prices()


def llm_price(model: str, on: date) -> tuple[float, float] | None:
    for valid_from, price_in, price_out in LLM_PRICES.get(model, []):
        if valid_from is None or on >= valid_from:
            return price_in, price_out
    return None


@dataclass
class CostItem:
    kind: str  # "llm" | "tts" | "clone"
    model: str
    usd: float | None
    detail: str
    estimated: bool


class CostTracker:
    """Collects cost items; safe to use from TTS worker threads."""

    def __init__(self) -> None:
        self.items: list[CostItem] = []
        self._lock = threading.Lock()

    def _add(self, item: CostItem) -> None:
        with self._lock:
            self.items.append(item)

    def add_llm(self, model: str, input_tokens: int, output_tokens: int) -> None:
        price = llm_price(model, date.today())
        usd = None
        if price:
            usd = (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
        detail = f"{input_tokens:,} in / {output_tokens:,} out tokens"
        self._add(CostItem("llm", model, usd, detail, price is None))

    def add_replicate(
        self, model: str, kind: str, chars: int = 0, predict_time: float | None = None
    ) -> None:
        spec, verified = REPLICATE_PRICES.get(model, ("", False))
        mode, _, rate_text = spec.partition(":")
        try:
            rate = float(rate_text)
        except ValueError:
            mode, rate = "", 0.0
        usd = None
        if mode == "per_char":
            usd, detail = chars * rate, f"{chars:,} chars"
        elif mode == "per_second" and predict_time is not None:
            usd, detail = predict_time * rate, f"{predict_time:.1f}s GPU"
        elif mode == "per_run":
            usd, detail = rate, "1 run"
        else:
            detail = f"{chars:,} chars, no price configured"
        self._add(CostItem(kind, model, usd, detail, not verified or usd is None))

    def summary(self) -> dict:
        """Aggregate per model/kind for storage on the broadcast document."""
        with self._lock:
            items = list(self.items)
        grouped: dict[tuple[str, str], dict] = {}
        for item in items:
            key = (item.kind, item.model)
            entry = grouped.setdefault(
                key,
                {"kind": item.kind, "model": item.model, "usd": 0.0, "calls": 0,
                 "estimated": False, "details": []},
            )
            entry["usd"] += item.usd or 0.0
            entry["calls"] += 1
            entry["estimated"] |= item.estimated
            if len(entry["details"]) < 3:
                entry["details"].append(item.detail)

        def total(kinds: tuple[str, ...]) -> float:
            return round(sum(e["usd"] for e in grouped.values() if e["kind"] in kinds), 4)

        return {
            "total_usd": total(("llm", "tts", "clone")),
            "llm_usd": total(("llm",)),
            "tts_usd": total(("tts", "clone")),
            "estimated": any(e["estimated"] for e in grouped.values()),
            "items": [{**e, "usd": round(e["usd"], 4)} for e in grouped.values()],
        }
