from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class DailyUsage:
    date: date
    source: str
    machine_id: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        integer_fields = (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "total_tokens",
            "request_count",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")
        if not self.source:
            raise ValueError("source must not be empty")


@dataclass(frozen=True)
class AggregatedUsage:
    date: date
    ccusage_tokens: int
    cpa_tokens: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    request_count: int
    cost_usd: float
