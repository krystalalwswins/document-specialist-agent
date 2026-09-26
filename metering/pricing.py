"""Versioned model price tables used only for local cost estimates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings


ONE_MILLION = Decimal("1000000")


@dataclass(frozen=True)
class ModelPrice:
    """USD rates per one million tokens for one model."""

    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def estimate(
        self, *, prompt_tokens: int, completion_tokens: int, cache_tokens: int
    ) -> Decimal:
        cached = min(max(cache_tokens, 0), max(prompt_tokens, 0))
        uncached = max(prompt_tokens - cached, 0)
        return (
            Decimal(uncached) * self.input_usd_per_million
            + Decimal(cached) * self.cached_input_usd_per_million
            + Decimal(max(completion_tokens, 0)) * self.output_usd_per_million
        ) / ONE_MILLION


class PricingCatalog:
    """Immutable price-table snapshot identified by an explicit version."""

    def __init__(
        self,
        version: str,
        prices: dict[str, ModelPrice] | None = None,
        *,
        currency: str = "USD",
    ) -> None:
        self.version = version.strip() or "unconfigured"
        self.currency = currency
        self._prices = dict(prices or {})

    @classmethod
    def from_settings(cls, settings: "Settings") -> "PricingCatalog":
        rates = (
            settings.llm_input_price_usd_per_million,
            settings.llm_cached_input_price_usd_per_million,
            settings.llm_output_price_usd_per_million,
        )
        if any(rate is None for rate in rates):
            return cls("unconfigured")
        return cls(
            settings.llm_pricing_version,
            {
                settings.llm_model: ModelPrice(
                    input_usd_per_million=rates[0],
                    cached_input_usd_per_million=rates[1],
                    output_usd_per_million=rates[2],
                )
            },
        )

    def price_for(self, model: str) -> ModelPrice | None:
        return self._prices.get(model)
