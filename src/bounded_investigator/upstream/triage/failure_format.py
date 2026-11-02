"""Human-facing formatting for provider/task failure counters.

This module contains no triage decisions; it only removes zero-value presentation noise
so Rich and plain summaries stay consistent.
"""

from __future__ import annotations


def format_failure_counters(items) -> str:
    visible = [(str(name), int(value)) for name, value in items if int(value) != 0]
    if not visible:
        return "无"
    return "  ".join("%s=%d" % (name, value) for name, value in visible)
