"""Per-variant token / cost budget. Charged on every model call, enforced before the next one."""

from __future__ import annotations

from ..domain.schemas import Usage
from .errors import BudgetExceeded


class Budget:
    def __init__(self, max_tokens: int, max_cost_cny: float, used: Usage | None = None):
        self.max_tokens = max_tokens
        self.max_cost_cny = max_cost_cny
        self.used = used or Usage()

    # called after every model call; raising here stops the step before the next call
    def charge(self, usage: Usage) -> None:
        self.used = self.used + usage
        if self.exhausted:
            raise BudgetExceeded(
                f"variant budget exhausted: {self.used.total_tokens}/{self.max_tokens} tokens, "
                f"CNY {self.used.cost_cny:.4f}/{self.max_cost_cny}")

    @property
    def exhausted(self) -> bool:
        return self.used.total_tokens > self.max_tokens or self.used.cost_cny > self.max_cost_cny
