"""PROJECTED cost. The free tier incurs none; these figures are what the same traffic would cost
on a paid tier at the configured prices. Never label them 'cost incurred'."""

from dataclasses import dataclass

from app.providers.base import Usage


@dataclass(frozen=True)
class ProjectedCost:
    prompt_tokens: int
    completion_tokens: int
    usd: float
    label: str = "projected, not incurred"


def projected_cost(usage: Usage, price_per_1k_in: float, price_per_1k_out: float) -> ProjectedCost:
    p, c = usage.prompt_tokens or 0, usage.completion_tokens or 0
    return ProjectedCost(p, c, p / 1000 * price_per_1k_in + c / 1000 * price_per_1k_out)
