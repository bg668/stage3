from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..schemas import UsageSummary

REASONING_DETAIL_KEYS = ("reasoning_tokens", "thinking_tokens", "thoughts_tokens")


def reasoning_tokens_from_usage(usage: Any) -> int:
    """Read provider-specific reasoning/thinking token detail keys without double-counting output total."""
    details = getattr(usage, "details", None) or {}
    if not isinstance(details, dict):
        try:
            details = dict(details)
        except Exception:
            return 0
    total = 0
    for key in REASONING_DETAIL_KEYS:
        value = details.get(key, 0) or 0
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
    return total


def usage_summary(usage: Any) -> UsageSummary:
    cost = getattr(usage, "cost", None)
    cost_str = str(cost) if isinstance(cost, Decimal) else (None if cost is None else str(cost))
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    reasoning_tokens = reasoning_tokens_from_usage(usage)
    return UsageSummary(
        requests=int(getattr(usage, "requests", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        visible_output_tokens=max(output_tokens - reasoning_tokens, 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
        cost_usd=cost_str,
    )


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def usage_delta(before: UsageSummary, after: UsageSummary) -> UsageSummary:
    before_cost = _decimal(before.cost_usd)
    after_cost = _decimal(after.cost_usd)
    cost = None
    if before_cost is not None and after_cost is not None:
        cost = str(after_cost - before_cost)
    return UsageSummary(
        requests=max(after.requests - before.requests, 0),
        input_tokens=max(after.input_tokens - before.input_tokens, 0),
        output_tokens=max(after.output_tokens - before.output_tokens, 0),
        reasoning_tokens=max(after.reasoning_tokens - before.reasoning_tokens, 0),
        visible_output_tokens=max(after.visible_output_tokens - before.visible_output_tokens, 0),
        total_tokens=max(after.total_tokens - before.total_tokens, 0),
        cache_read_tokens=max(after.cache_read_tokens - before.cache_read_tokens, 0),
        cache_write_tokens=max(after.cache_write_tokens - before.cache_write_tokens, 0),
        cost_usd=cost,
    )


def merge_usage_summaries(*items: UsageSummary) -> UsageSummary:
    """Sum independent usage buckets without double-counting reasoning inside output_tokens."""
    costs = [_decimal(item.cost_usd) for item in items]
    cost = None
    if items and all(c is not None for c in costs):
        cost = str(sum((c for c in costs if c is not None), Decimal("0")))
    output_tokens = sum(item.output_tokens for item in items)
    reasoning_tokens = sum(item.reasoning_tokens for item in items)
    return UsageSummary(
        requests=sum(item.requests for item in items),
        input_tokens=sum(item.input_tokens for item in items),
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        visible_output_tokens=max(output_tokens - reasoning_tokens, 0),
        total_tokens=sum(item.total_tokens for item in items),
        cache_read_tokens=sum(item.cache_read_tokens for item in items),
        cache_write_tokens=sum(item.cache_write_tokens for item in items),
        cost_usd=cost,
    )
